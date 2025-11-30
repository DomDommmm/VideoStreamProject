from tkinter import *
import tkinter.messagebox as tkMessageBox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os
import time
from collections import deque

from RtpPacket import RtpPacket

# Jitter buffer configuration
PREBUFFER_MIN = 10            # Minimum frames to accumulate before starting playback
PREBUFFER_TIMEOUT = 2.0       # Max seconds to wait for prebuffer
TARGET_FPS = 30.0             # Desired playback framerate
MAX_BUFFER = 120              # Cap buffer length (avoid memory growth)
STALL_MODE = "wait"           # "wait" or "drop" when buffer underflows
MAX_FRAME_BYTES = 2_000_000   # Safety cap per frame size

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT = ".jpg"

class FrameBuffer:
	def __init__(self, maxlen):
		self._dq = deque()
		self._lock = threading.Lock()
		self._maxlen = maxlen
		self.dropped_overflow = 0

	def push(self, frame_bytes):
		with self._lock:
			if len(self._dq) >= self._maxlen:
				# Drop newest to keep continuity (alternatively popleft to drop oldest)
				self.dropped_overflow += 1
				return False
			self._dq.append(frame_bytes)
			return True
		
	def pop(self):
		with self._lock:
			if self._dq:
				return self._dq.popleft()
			return None
	
	def size(self):
		with self._lock:
			return len(self._dq)
	
	def clear(self):
		with self._lock:
			self._dq.clear()
	
	def stats(self):
		with self._lock:
			return {
				"size": len(self._dq),
				"dropped_overflow": self.dropped_overflow
			}

class Client:
	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT
	
	SETUP = 0
	PLAY = 1
	PAUSE = 2
	TEARDOWN = 3
	
	# Initiation..
	def __init__(self, master, serveraddr, serverport, rtpport, filename):
		self.master = master
		self.master.protocol("WM_DELETE_WINDOW", self.handler)
		self.createWidgets()
		self.serverAddr = serveraddr
		self.serverPort = int(serverport)
		self.rtpPort = int(rtpport)
		self.fileName = filename
		self.rtspSeq = 0
		self.sessionId = 0
		self.requestSent = -1
		self.teardownAcked = 0
		self.connectToServer()
		self.frameNbr = 0
		self.frameBuffer = bytearray()
		self.lastSeq = None
		self.seqnum = 0 # RTP sequ
		self._rtspClosing = False
		# JPEG markers for basic integrity checks
		self._SOI = b"\xff\xd8"
		self._EOI = b"\xff\xd9"
		self._MAX_FRAME_BYTES = 2 * 1024 * 1024  # 2MB safety cap
		
		self.buffer = FrameBuffer(MAX_BUFFER)
		self.playbackThread = None
		self.stopPlayback = threading.Event()
		
	def createWidgets(self):
		"""Build GUI."""
		# Create Setup button
		self.setup = Button(self.master, width=20, padx=3, pady=3)
		self.setup["text"] = "Setup"
		self.setup["command"] = self.setupMovie
		self.setup.grid(row=1, column=0, padx=2, pady=2)
		
		# Create Play button		
		self.start = Button(self.master, width=20, padx=3, pady=3)
		self.start["text"] = "Play"
		self.start["command"] = self.playMovie
		self.start.grid(row=1, column=1, padx=2, pady=2)
		
		# Create Pause button			
		self.pause = Button(self.master, width=20, padx=3, pady=3)
		self.pause["text"] = "Pause"
		self.pause["command"] = self.pauseMovie
		self.pause.grid(row=1, column=2, padx=2, pady=2)
		
		# Create Teardown button
		self.teardown = Button(self.master, width=20, padx=3, pady=3)
		self.teardown["text"] = "Teardown"
		self.teardown["command"] = self.exitClient
		self.teardown.grid(row=1, column=3, padx=2, pady=2)
		
		# Create a label to display the movie
		self.label = Label(self.master)
		self.label.grid(row=0, column=0, columnspan=4, sticky=W+E+N+S, padx=5, pady=5)

		# Make grid responsive
		try:
			for c in range(4):
				self.master.columnconfigure(c, weight=1)
			self.master.rowconfigure(0, weight=1)
		except Exception:
			pass

	def exitClient(self):
		"""Immediately stop streaming, close sockets, and exit the app."""
		# Signal listener to stop
		try:
			if hasattr(self, "playEvent") and self.playEvent:
				self.playEvent.set()
		except Exception:
			pass
		self.teardownAcked = 1  # make listenRtp break on next timeout

		# Best-effort TEARDOWN (don’t block on it)
		try:
			if self.state != self.INIT:
				self.sendRtspRequest(self.TEARDOWN)
		except Exception:
			pass

		# Close RTP socket
		try:
			if getattr(self, "rtpSocket", None):
				self.rtpSocket.close()
		except Exception:
			pass

		# Close RTSP socket
		try:
			self._rtspClosing = True
			if getattr(self, "rtspSocket", None):
				self.rtspSocket.close()
		except Exception:
			pass

		# Remove cache file (ignore errors)
		try:
			cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
			if os.path.exists(cachename):
				os.remove(cachename)
		except Exception:
			pass

		# Break Tk mainloop and destroy window
		try:
			self.master.quit()      # stops mainloop()
		except Exception:
			pass
		try:
			self.master.destroy()   # closes window
		except Exception:
			pass

		# Ensure process exit (in case any non-daemon threads are still around)
		try:
			sys.exit(0)
		except SystemExit:
			pass

	def handler(self):
		"""Immediate quit on window close."""
		self.exitClient()
	
	def setupMovie(self):
		"""Setup button handler."""
		if self.state == self.INIT:
			self.sendRtspRequest(self.SETUP)
	
	def pauseMovie(self):
		"""Pause button handler."""
		if self.state == self.PLAYING:
			self.sendRtspRequest(self.PAUSE)
	
	def playMovie(self):
		"""Play button handler."""
		if self.state == self.READY:
			# Create a new thread to listen for RTP packets
			self.frameBuffer = bytearray()
			self.lastSeq = None
			self.buffer.clear()
			self.stopPlayback.clear()

			threading.Thread(target=self.listenRtp, daemon=True).start()
			self.playEvent = threading.Event()
			self.playEvent.clear()

			# Send PLAY RTSP
			self.sendRtspRequest(self.PLAY)
			
			self.playbackThread = threading.Thread(target=self.displayLoop, daemon=True)
			self.playbackThread.start()
	
	def listenRtp(self):		
		"""Listen for RTP packets."""
		timeouts = 0
		while True:
			try:
				data = self.rtpSocket.recv(20480)
				if data:
					timeouts = 0
					rtpPacket = RtpPacket()
					rtpPacket.decode(data)

					seq = rtpPacket.seqNum()
					# Marker bit: top bit of header[1]
					marker = rtpPacket.marker()
					payload = rtpPacket.getPayload()

					# Gap detection: if a fragment is missing, drop the partial frame and resync
					if self.lastSeq is not None and seq != self.lastSeq + 1:
						print(f"Gap detected: lastSeq={self.lastSeq}, currentSeq={seq}. Dropping partial frame.")
						self.frameBuffer.clear()
						self.lastSeq = None

					# If we are not currently collecting a frame, only start when the chunk begins with JPEG SOI
					if len(self.frameBuffer) == 0 and not payload.startswith(self._SOI):
						# Not a frame start; skip until we find SOI to resync
						self.lastSeq = seq
						continue
				
					# Append chunk
					self.frameBuffer.extend(payload)
					self.lastSeq = seq

					# Safety cap: if the frame grows too large, drop it to avoid runaway memory
					if len(self.frameBuffer) > self._MAX_FRAME_BYTES:
						print(f"[Client] Frame exceeded {self._MAX_FRAME_BYTES} bytes. Dropping.")
						self.frameBuffer.clear()
						self.lastSeq = None
						continue

					# If marker == 1, we're reached the end of this frame
					if marker == 1:
						# Validate basic JPEG integrity (SOI/EOI)
						if not (len(self.frameBuffer) >= 4 and self.frameBuffer.startswith(self._SOI) and self.frameBuffer.endswith(self._EOI)):
							print(f"[Client] Discarding incomplete frame (size={len(self.frameBuffer)}). Missing SOI/EOI.")
							self.frameBuffer.clear()
							self.lastSeq = None
							continue
						try:
							print(f"[Client] Assembled frame of {len(self.frameBuffer)} bytes (last seq={seq})")
							imageFile = self.writeFrame(bytes(self.frameBuffer))
							self.updateMovie(imageFile)
						except Exception as pe:
							print(f"[Client] Failed to display frame: {pe}")
						finally:
							self.frameBuffer.clear()
							self.lastSeq = None
					
					# currFrameNbr = rtpPacket.seqNum()
					# # Basic drop detection
					# if currFrameNbr > self.frameNbr + 1:
					# 	print(f"Warning: dropped {currFrameNbr - self.frameNbr - 1} frame(s) between {self.frameNbr} -> {currFrameNbr}")
					# print("Current Seq Num: " + str(currFrameNbr))
										
					# if currFrameNbr > self.frameNbr: # Discard the late packet
					# 	self.frameNbr = currFrameNbr
					# 	self.updateMovie(self.writeFrame(rtpPacket.getPayload()))
			except socket.timeout:
				# Timeout is expected; check control flags and continue or exit
				if self.playEvent.isSet() or self.teardownAcked == 1:
					break
				timeouts += 1
				if timeouts % 10 == 0:
					print("[Client] No RTP packets received yet...")
				continue
			except Exception as e:
				# Stop listening upon requesting PAUSE or TEARDOWN
				if self.playEvent.isSet(): 
					break
				
				# Upon receiving ACK for TEARDOWN request,
				# close the RTP socket
				if self.teardownAcked == 1:
					try:
						self.rtpSocket.close()
					except Exception:
						pass
					break
				# Log unexpected errors to help diagnose remote issues
				print(f"[Client] listenRtp error: {e}")
				traceback.print_exc()
					
	def writeFrame(self, data):
		"""Write the received frame to a temp image file. Return the image file."""
		cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
		file = open(cachename, "wb")
		file.write(data)
		file.close()
		
		return cachename
	
	def updateMovie(self, imageFile):
		"""Update the image file as video frame in the GUI."""
		img = Image.open(imageFile)
		photo = ImageTk.PhotoImage(img)
		self.label.configure(image=photo)
		self.label.image = photo
		# Optionally resize the window to fit the video frame plus controls
		try:
			w, h = img.size
			# Approximate controls height padding
			pad = 80
			self.master.geometry(f"{max(w, 300)}x{h + pad}")
		except Exception:
			pass
		
	def connectToServer(self):
		"""Connect to the Server. Start a new RTSP/TCP session."""
		self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			self.rtspSocket.connect((self.serverAddr, self.serverPort))
		except:
			tkMessageBox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' %self.serverAddr)
	
	def sendRtspRequest(self, requestCode):
		"""Send RTSP request to the server."""
		
		# Setup request
		if requestCode == self.SETUP and self.state == self.INIT:
			# before sending SETUP
			threading.Thread(target=self.recvRtspReply, daemon=True).start()
			
			# Update RTSP sequence number.
			self.rtspSeq += 1
			
			# Write the RTSP request to be sent (standard RTSP format).
			request = (
				f"SETUP {self.fileName} RTSP/1.0\n"
				f"CSeq: {self.rtspSeq}\n"
				f"Transport: RTP/UDP; client_port={self.rtpPort}\n\n"
			)
			
			# Keep track of the sent request.
			self.requestSent = self.SETUP
		
		# Play request
		elif requestCode == self.PLAY and self.state == self.READY:
			# Update RTSP sequence number.
			self.rtspSeq += 1
			
			# Write the RTSP request to be sent (standard RTSP format).
			request = (
				f"PLAY {self.fileName} RTSP/1.0\n"
				f"CSeq: {self.rtspSeq}\n"
				f"Session: {self.sessionId}\n\n"
			)
			
			# Keep track of the sent request.
			self.requestSent = self.PLAY
		
		# Pause request
		elif requestCode == self.PAUSE and self.state == self.PLAYING:
			# Update RTSP sequence number.
			self.rtspSeq += 1
			
			# Write the RTSP request to be sent (standard RTSP format).
			request = (
				f"PAUSE {self.fileName} RTSP/1.0\n"
				f"CSeq: {self.rtspSeq}\n"
				f"Session: {self.sessionId}\n\n"
			)
			
			# Keep track of the sent request.
			self.requestSent = self.PAUSE
			
		# Teardown request
		elif requestCode == self.TEARDOWN and not self.state == self.INIT:
			# Update RTSP sequence number.
			self.rtspSeq += 1
			
			# Write the RTSP request to be sent (standard RTSP format).
			request = (
				f"TEARDOWN {self.fileName} RTSP/1.0\n"
				f"CSeq: {self.rtspSeq}\n"
				f"Session: {self.sessionId}\n\n"
			)
			
			# Keep track of the sent request.
			self.requestSent = self.TEARDOWN
		else:
			return
		
		# Send the RTSP request using rtspSocket.
		try:
			self.rtspSocket.send(request.encode())
			print('\nData sent:\n' + request)
		except Exception as e:
			tkMessageBox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' %self.serverAddr)
			print("Exception: " + str(e))
	
	def recvRtspReply(self):
		"""Receive RTSP reply from the server."""
		while True:
			try:
				reply = self.rtspSocket.recv(1024)
			except OSError as e:
				# Socket likely closed elsewhere during teardown; exit quietly
				if not getattr(self, "_rtspClosing", False):
					print(f"[Client] recvRtspReply socket closed: {e}")
				break
			
			if reply:
				try:
					self.parseRtspReply(reply.decode("utf-8"))
				except Exception as pe:
					print(f"[Client] Failed to parse RTSP reply: {pe}")
			
			# Close the RTSP socket upon requesting Teardown
			if self.requestSent == self.TEARDOWN:
				try:
					self.rtspSocket.shutdown(socket.SHUT_RDWR)
				except Exception:
					pass
				try:
					self.rtspSocket.close()
				except Exception:
					pass
				break
	
	def parseRtspReply(self, data):
		"""Parse the RTSP reply from the server."""
		lines = data.split('\n')
		seqNum = int(lines[1].split(' ')[1])
		
		# Process only if the server reply's sequence number is the same as the request's
		if seqNum == self.rtspSeq:
			session = int(lines[2].split(' ')[1])
			# New RTSP session ID
			if self.sessionId == 0:
				self.sessionId = session
			
			# Process only if the session ID is the same
			if self.sessionId == session:
				if int(lines[0].split(' ')[1]) == 200: 
					if self.requestSent == self.SETUP:
						# Update RTSP state.
						self.state = self.READY
				
						# Open RTP port.
						self.openRtpPort() 
					elif self.requestSent == self.PLAY:
						self.state = self.PLAYING
					elif self.requestSent == self.PAUSE:
						self.state = self.READY
						
						# The play thread exits. A new thread is created on resume.
						self.playEvent.set()
					elif self.requestSent == self.TEARDOWN:
						self.state = self.INIT
						
						# Flag the teardownAcked to close the socket.
						self.teardownAcked = 1 
	
	def openRtpPort(self):
		"""Open RTP socket binded to a specified port."""
		#-------------
		# TO COMPLETE
		#-------------
		# Create a new datagram socket to receive RTP packets from the server
		self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		# Increase receive buffer to reduce UDP drops
		try:
			self.rtpSocket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
		except Exception:
			pass

		
		# Set the timeout value of the socket to 0.5sec
		self.rtpSocket.settimeout(0.5)
		
		try:
			# Bind the socket to the address using the RTP port given by the client user
			self.rtpSocket.bind(('', self.rtpPort))
			# Read back OS-assigned buffer to confirm
			try:
				bufsz = self.rtpSocket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
				print(f"Binded RTP port: {self.rtpPort} (SO_RCVBUF={bufsz} bytes)")
			except Exception:
				print(f"Binded RTP port: {self.rtpPort}")
		except OSError as e:
			tkMessageBox.showwarning('Unable to Bind', 'Unable to bind PORT=%d' %self.rtpPort)
			try:
				self.rtpSocket.close()
			except:
				pass
			self.rtpSocket = None

	# Removed duplicate handler below; the top-level handler attached in __init__ is used
