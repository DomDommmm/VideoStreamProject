from tkinter import *
import tkinter.messagebox as tkMessageBox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os
import time
from collections import deque

from RtpPacket import RtpPacket

# Jitter buffer configuration
PREBUFFER_MIN = 50            # Minimum frames to accumulate before starting playback
PREBUFFER_TIMEOUT = 3.0       # Max seconds to wait for prebuffer
TARGET_FPS = 30.0             # Desired playback framerate
MAX_BUFFER = 450              # Cap buffer length (avoid memory growth)

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT = ".jpg"

class FrameBuffer:
	def __init__(self, maxlen):
		self.dq = deque()
		self.lock = threading.Lock()
		self.maxlen = maxlen
		self.dropped_overflow = 0

	def push(self, frame_bytes):
		with self.lock:
			if len(self.dq) >= self.maxlen:
				# Drop newest to keep continuity (alternatively popleft to drop oldest)
				self.dropped_overflow += 1
				self.dq.popleft()
				return False
			self.dq.append(frame_bytes)
			return True
		
	def pop(self):
		with self.lock:
			if self.dq:
				return self.dq.popleft()
			return None
	
	def size(self):
		with self.lock:
			return len(self.dq)
	
	def clear(self):
		with self.lock:
			self.dq.clear()
	
	def stats(self):
		with self.lock:
			return {
				"size": len(self.dq),
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
	RESOLUTION = 4
	
	# Initiation..
	def __init__(self, master, serveraddr, serverport, rtpport, filename):
		self.master = master
		self.master.protocol("WM_DELETE_WINDOW", self.handler)
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
		self.playEvent = threading.Event()
		# Resolution control presets must be initialized before widgets
		self.res_presets = {
			"Original": None,
			"144p": 144,
			"240p": 240,
			"360p": 360,
			"480p": 480,
			"720p": 720,
			"1080p": 1080
		}
		self.target_height = None
		self.res_var = None
		self.last_frame_size = None
		self.base_display_size = None # (w, h) window-fit set on first frame
		self.createWidgets()
		self.seqnum = 0 # RTP seq
		self._rtspClosing = False
		# JPEG markers for basic integrity checks (instance-level, Option B)
		self.SOI = b"\xff\xd8"
		self.EOI = b"\xff\xd9"
		self.MAX_FRAME_BYTES = 2 * 1024 * 1024  # 2MB safety cap
		
		self.buffer = FrameBuffer(MAX_BUFFER)
		self.playbackThread = None
		self.stopPlayback = threading.Event()

		self.paused = False
		self.resync = False

		self.resume_until = 0.0      # time.monotonic() deadline to ignore non-SOI
		self.resume_grace_ms = 200   # milliseconds to ignore non-SOI packets

		self.assembled_count = 0
		self.displayed_count = 0

		# self.FEC_GROUP_SIZE = 10
		self.RTP_PT_MJPEG = 26
		self.RTP_PT_FEC = 127

		self.fec_blocks = {}
		# self.fec_next_block_id = 0
		# self.fec_next_index = 0
		# Grace wait (ms) before recovering the last packet in a block
		# Helps avoid false recoveries when parity arrives before data due to UDP reordering
		self.fec_last_wait_ms = 18

		# === Network stats for adaptation ===
		self.last_rx_seq = None				# last seen RTP seq (data only)
		self.rx_expected = 0				# expected packets (by seq)
		self.rx_received = 0				# received packets
		self.rx_lost_seq = 0				# lost packets (gaps by seq)
		self.fec_recovered_packets = 0		# packets recovered by FEC
		self.buffer_underruns = 0			# number of times buffer ran empty during playback
		self.net_profile = "HIGH"			# Network profile: HIGH, MED, LOW
		self.adapt_stop = threading.Event()
		self.adapt_thread = None

	@staticmethod
	def fecParseHeader(buf):
		"""Return (block_id, group_size, fec_len)"""
		if len(buf) < 8:
			return (None, None, None)
		block_id = int.from_bytes(buf[0:4], 'big')
		group_size = int.from_bytes(buf[4:6], 'big')
		fec_len = int.from_bytes(buf[6:8], 'big')
		return (block_id, group_size, fec_len)

	@staticmethod
	def _xor_bytes(a, b):
		"""XOR two byte strings, padding shorter one with zeros"""
		L = max(len(a), len(b))
		if len(a) < L:
			a = a + b'\x00' * (L - len(a))
		if len(b) < L:
			b = b + b'\x00' * (L - len(b))
		return bytes(x ^ y for x, y in zip(a, b))
		
	def createWidgets(self):
		"""Build GUI."""
		# Create Setup button
		self.setup = Button(self.master, width=20, padx=3, pady=3)
		self.setup["text"] = "Setup"
		self.setup["command"] = self.setupMovie
		self.setup.grid(row=1, column=0, padx=2, pady=2)
		
		# Create Play button		
		self.start = Button(self.master, width=20, padx=3, pady=3, text="Play", command=self.playMovie)
		self.start.grid(row=1, column=1, padx=2, pady=2)

		# Resolution selector between Play and Pause
		self.res_var = StringVar(self.master)
		self.res_var.set("Original")  # default
		_res_labels = list(self.res_presets.keys())

		# Create OptionMenu with command callback
		self.res_select = OptionMenu(self.master, self.res_var, *_res_labels, command=self.onResolutionSelected)
		self.res_select.configure(width=16)
		self.res_select.grid(row=1, column=2, padx=2, pady=2)

		# Create Pause button
		self.pause = Button(self.master, width=20, padx=3, pady=3, text="Pause", command=self.pauseMovie)
		self.pause.grid(row=1, column=3, padx=2, pady=2)

		# Create Teardown button
		self.teardown = Button(self.master, width=20, padx=3, pady=3, text="Teardown", command=self.exitClient)
		self.teardown.grid(row=1, column=4, padx=2, pady=2)

		# Label spans 5 columns now
		self.label = Label(self.master)
		self.label.grid(row=0, column=0, columnspan=5, sticky=W+E+N+S, padx=5, pady=5)

		# Make grid responsive for 5 columns
		try:
			for c in range(5):
				self.master.columnconfigure(c, weight=1)
			self.master.rowconfigure(0, weight=1)
		except Exception:
			pass

	def setResolutionUI(self, label):
		"""
		Update resolution dropdown selection in the UI.
		label must be one of self.res_presets keys, e.g. 'Original', '480p', etc.
		"""
		if not self.master:
			return
		def _do():
			if label in self.res_presets:
				self.res_var.set(label)
			
		try:
			self.master.after(0, _do)
		except Exception as e:
			print(f"[Client] setResolutionUI error: {e}")

	def exitClient(self):
		"""Immediately stop streaming, close sockets, and exit the app."""
		# Signal listener to stop
		self.adapt_stop.set()
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

	def showLoading(self, on=True):
		"""Toggle a simple loading overlay when waiting for frame."""
		try:
			if on:
				self.label.configure(text="Loading...", compound="center")
			else:
				self.label.configure(text="", compound=None)
		except Exception:
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
			self.playEvent.set()
			self.stopPlayback.set()
			self.adapt_stop.set()
			self.paused = True
	
	def playMovie(self):
		"""Play button handler."""
		if self.state == self.READY:
			# Create a new thread to listen for RTP packets
			self.frameBuffer = bytearray()
			self.lastSeq = None
			self.stopPlayback.clear()

			self.playEvent.clear()
			threading.Thread(target=self.listenRtp, daemon=True).start()

			# Send PLAY RTSP
			self.sendRtspRequest(self.PLAY)
			
			self.playbackThread = threading.Thread(target=self.displayLoop, daemon=True)
			self.playbackThread.start()

			# start adaptation loop
			self.adapt_stop.clear()
			if self.adapt_thread is None or not self.adapt_thread.is_alive():
				self.adapt_thread = threading.Thread(target=self.adaptationLoop, daemon=True)
				self.adapt_thread.start()

	def onResolutionSelected(self, label):
		"""
		Handle selection from the resolution dropdown;
		send RESOLUTION RTSP.
		"""
		height = self.res_presets.get(label, None)
		self.target_height = height
		
		# Only send if we have a valid RTSP session ID (After SETUP)
		if self.sessionId == 0:
			print(f"[Client] Resolution set locally to {label}")
			return
		
		self.rtspSeq += 1
		desired = 0 if height is None else int(height)
		request = (
			f"RESOLUTION {self.fileName} RTSP/1.0\n"
			f"CSeq: {self.rtspSeq}\n"
			f"Session: {self.sessionId}\n"
			f"X-Resolution: {desired}\n\n"
		)
		try:
			self.rtspSocket.send(request.encode())
			print(f"[Client] Requested resolution change to {label} ({desired or 'Original'}). CSeq={self.rtspSeq}")
			self.requestSent = self.RESOLUTION
		except Exception as e:
			print(f"[Client] Failed to send RESOLUTION: {e}")
	
	def listenRtp(self):		
		"""Receive RTP packets, reassemble frames, and push completed frames into jitter buffer."""
		timeouts = 0
		partial = bytearray()
		lastSeq = None

		while True:
			try:
				data = self.rtpSocket.recv(20480)
				if not data:
					continue
				timeouts = 0
				rtpPacket = RtpPacket()
				rtpPacket.decode(data)

				pt = rtpPacket.payloadType()
				seq = rtpPacket.seqNum()
				marker = rtpPacket.marker()
				payload = rtpPacket.getPayload()

				if pt == self.RTP_PT_FEC:
					fec_payload = rtpPacket.getPayload()
					hdr = self.fecParseHeader(fec_payload[:8])
					if not hdr:
						continue
					block_id, group_size, fec_len = hdr
					parity = fec_payload[8:8 + fec_len]
					
					block = self.fecGetBlock(block_id)
					# Validate or set group size
					if block['group_size'] is None:
						block['group_size'] = group_size
					elif block['group_size'] != group_size:
						block['chunks'].clear()
						block['recovered_idx'] = None
						block['recovered_at'] = None
						block['group_size'] = group_size
					block['parity'] = parity
					self.fecFlushBlock(block_id)
				elif pt == self.RTP_PT_MJPEG:
					# Update packet loss stats
					if self.last_rx_seq is None:
						self.last_rx_seq = seq
					else:
						delta = (seq - self.last_rx_seq) & 0xFFFF
						if delta > 0:
							if delta > 1:
								self.rx_lost_seq += (delta - 1)
							self.rx_expected += delta
							self.last_rx_seq = seq
						else:
							pass # out-of-order or duplicate
					self.rx_received += 1

					if len(payload) < 8:
						chunk = payload
						self.pushDataFragment(chunk, marker)
						continue

					block_id = int.from_bytes(payload[0:4], 'big')
					index = int.from_bytes(payload[4:6], 'big')
					group_size = int.from_bytes(payload[6:8], 'big')
					chunk = payload[8:]

					block = self.fecGetBlock(block_id)
					if block['group_size'] is None:
						block['group_size'] = group_size
					if block['group_size'] != group_size:
						block['chunks'].clear()
						block['parity'] = None
						block['group_size'] = group_size
						block['recovered_idx'] = None
						block['recovered_at'] = None


					if block.get('recorvered_idx') == index:
						continue

					block['chunks'][index] = chunk

					self.fecFlushBlock(block_id)
					

			# 	# Sequence gap handling
			# 	if lastSeq is not None and seq != lastSeq + 1:
			# 		# Drop partial frame
			# 		partial.clear()
			# 		lastSeq = None

			# 	# If paused, flush any in-flight assembly and wait
			# 	if self.paused:
			# 		partial.clear()
			# 		lastSeq = None
			# 		self.paused = False

			# 	# Resync after resume: ignore non-SOI until grace deadline or first clean SOI
			# 	if len(partial) == 0:
			# 		now = time.monotonic()
			# 		if self.resync:
			# 			if now <= self.resume_until:
			# 				if not payload.startswith(self.SOI):
			# 					lastSeq = None
			# 					continue
			# 			else:
			# 				if not payload.startswith(self.SOI):
			# 					lastSeq = None
			# 					continue
			# 		# First clean SOI found after resume
			# 		self.resync = False

			# 	partial.extend(payload)
			# 	lastSeq = seq

			# 	# Safety cap
			# 	if len(partial) > self.MAX_FRAME_BYTES:
			# 		print(f"[Client] Oversized frame ({len(partial)} bytes) dropped.")
			# 		partial.clear()
			# 		lastSeq = None
			# 		continue

			# 	if marker == 1:
			# 		# Validate SOI/EOI
			# 		if not (partial.startswith(self.SOI) and partial.endswith(self.EOI)):
			# 			print(f"[Client] Incomplete JPEG dropped size={len(partial)} seq={seq}")
			# 			partial.clear()
			# 			lastSeq = None
			# 			continue

			# 		size_before_clear = len(partial)
			# 		pushed = self.buffer.push(bytes(partial))
			# 		if pushed:
			# 			self.assembled_count += 1
			# 			print(f"[Client] Receive frame #{self.assembled_count} buf={self.buffer.size()} size={size_before_clear}")
			# 		else:
			# 			print("[Client] Buffer full; frame dropped.")
			# 		partial.clear()
			# 		lastSeq = None

			except socket.timeout:
				if self.playEvent.isSet() or self.teardownAcked == 1:
					break
				timeouts += 1
				if timeouts % 10 == 0:
					print("[Client] Waiting for RTP...")
				continue
			except Exception as e:
				if self.playEvent.isSet() or self.teardownAcked == 1:
					break
				print(f"[Client] listenRtp error: {e}")
				traceback.print_exc()
				continue

	def pushDataFragment(self, payload, marker, seq=None):
		"""Assemble JPEG from RTP fragments. seq=None for recovered fragments."""
		try:
			# # Sequence gap handling for real RTP packets
			# if seq is not None and getattr(self, 'lastSeq', None) is not None and seq != self.lastSeq + 1:
			# 	# Gap: drop current partial
			# 	self.frameBuffer.clear()
			# 	self.lastSeq = None

			# Pause flush
			if self.paused:
				self.frameBuffer.clear()
				self.paused = False

			# Enforce SOI at frame start with resume grace
			if len(self.frameBuffer) == 0:
				if self.resync:
					now = time.monotonic()
					if now <= self.resume_until:
						if not payload.startswith(self.SOI):
							return
					else:
						if not payload.startswith(self.SOI):
							return
					self.resync = False
				else:
					if not payload.startswith(self.SOI):
						return

			# Append fragment
			self.frameBuffer.extend(payload)
			# if seq is not None:
			# 	self.lastSeq = seq

			# Safety cap
			if len(self.frameBuffer) > self.MAX_FRAME_BYTES:
				print(f"[Client] Oversized frame ({len(self.frameBuffer)} bytes) dropped.")
				self.frameBuffer.clear()
				# self.lastSeq = None
				return

			# End of frame
			if marker == 1 or payload.endswith(self.EOI):
				if not (self.frameBuffer.startswith(self.SOI) and self.frameBuffer.endswith(self.EOI)):
					print(f"[Client] Incomplete JPEG dropped size={len(self.frameBuffer)}")
					self.frameBuffer.clear()
					self.lastSeq = None
					return

				size_before_clear = len(self.frameBuffer)
				pushed = self.buffer.push(bytes(self.frameBuffer))
				if pushed:
					self.assembled_count += 1
					print(f"[Client] Receive frame #{self.assembled_count} buf={self.buffer.size()} size={size_before_clear}")
				else:
					print("[Client] Buffer full; frame dropped.")
				self.frameBuffer.clear()
				# self.lastSeq = None
		except Exception as e:
			print(f"[Client] pushDataFragment error: {e}")
			self.frameBuffer.clear()
			# self.lastSeq = None

	def writeFrame(self, data):
		"""Write the received frame to a temp image file. Return the image file."""
		cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
		file = open(cachename, "wb")
		file.write(data)
		file.close()
		
		return cachename
	
	def updateMovie(self, imageFile):
		"""
		Update GUI frame;
		Upscale to the initial window size (zoom) if needed.
		"""
		img = Image.open(imageFile)
		src_w, src_h = img.size
		# Initialize baseline window frame size from first frame
		if self.last_frame_size is None:
			self.last_frame_size = (src_w, src_h)
			# Set initial window geometry once:
			try:
				pad = 80
				self.master.geometry(f"{max(src_w, 300)}x{src_h + pad}")
			except Exception:
				pass
		target_w, target_h = self.last_frame_size
		# Always scale incoming frames to target window size (zoom if smaller)
		if (src_w, src_h) != (target_w, target_h):
			img = img.resize((target_w, target_h), Image.BILINEAR)
		photo = ImageTk.PhotoImage(img)
		self.label.configure(image=photo)
		self.label.image=photo
		
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
						# Reset stats for a fresh session
						self.assembled_count = 0
						self.displayed_count = 0
				
						# Open RTP port.
						self.openRtpPort() 
					elif self.requestSent == self.PLAY:
						self.state = self.PLAYING
						self.resync = True
						now = time.monotonic()
						self.resume_until = now + (self.resume_grace_ms / 1000.0)
					elif self.requestSent == self.PAUSE:
						self.state = self.READY
						self.playEvent.set()
						self.paused = True
						
						# The play thread exits. A new thread is created on resume.
						self.playEvent.set()
					elif self.requestSent == self.RESOLUTION:
						print("[Client] Resolution change acknowledged by server.")
					elif self.requestSent == self.TEARDOWN:
						self.state = self.INIT
						
						# Flag the teardownAcked to close the socket.
						self.teardownAcked = 1 
	
	def openRtpPort(self):
		"""Open RTP socket binded to a specified port."""
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

	# Removed duplicate handler below;

	def displayLoop(self):
		"""Consume frames from buffer at TARGET_FPS, starting only after prebuffer threshold."""
		start_wait = time.monotonic()
		# Wait for prebuffer or timeout
		while self.buffer.size() < PREBUFFER_MIN:
			if (time.monotonic() - start_wait) > PREBUFFER_TIMEOUT:
				print("[Client] Prebuffer timeout; starting playback with fewer frames.")
				break
			if self.stopPlayback.is_set() or self.teardownAcked == 1:
				self.showLoading(False)
				return
			time.sleep(0.01)
		self.showLoading(False)

		print(f"[Client] Playback starting. Initial buffered frames={self.buffer.size()}")

		frame_interval = 1.0 / TARGET_FPS
		now = time.monotonic()
		next_display_time = time.monotonic()

		displayed = 0
		# dropped_empty = 0
		# last_log = time.monotonic()

		loading = False
		empty_since = None

		while not self.stopPlayback.is_set() and self.teardownAcked == 0:
			now = time.monotonic()
			frame_interval = 1.0 / TARGET_FPS
			delay = next_display_time - now
			if delay > 0:
				time.sleep(delay)

			frame = self.buffer.pop()
			if frame is None:
				if empty_since is None:
					empty_since = time.monotonic()
				while frame is None and not self.stopPlayback.is_set() and self.teardownAcked == 0:
					if not loading and (time.monotonic() - empty_since) > 1.5:
						self.showLoading(True)
						loading = True
						# count underrun once per event
						self.buffer_underruns += 1
						print(f"[Client] Buffer underrun #{self.buffer_underruns}")
					frame = self.buffer.pop()
					time.sleep(0.01)
				if loading:
					self.showLoading(False)
					loading = False
				empty_since = None

			if frame is None:
				# Playback stopping or still empty after wait
				continue

			try:
				imageFile = self.writeFrame(frame)
				self.updateMovie(imageFile)
				displayed += 1
			except Exception as e:
				print(f"[Client] display error: {e}")

			next_display_time += frame_interval

			self.displayed_count += 1
			stats = self.buffer.stats()
			print(f"[Client] Stats: displayed={self.displayed_count} buf={stats['size']} overflowDrops={stats['dropped_overflow']}")

	def fecGetBlock(self, block_id):
		block = self.fec_blocks.get(block_id)
		if not block:
			block = {
				'chunks': {},
				'group_size': None,
				'parity': None,
				'recovered_idx': None,
				'recovered_at': None,
				'flushed': False
			}
			self.fec_blocks[block_id] = block
		return block
	
	def fecRecoverPacket(self, block_id):
		block = self.fec_blocks.get(block_id)
		if not block:
			return None
		group = block.get('group_size')
		parity = block.get('parity')
		if not group or parity is None:
			return None

		if block.get('recovered_idx') is not None:
			idx = block['recovered_idx']
			return (idx, block['chunks'].get(idx))
		
		present = len(block['chunks'])
		if present != group - 1:
			return None
	
		have = [False] * group
		for idx in block['chunks'].keys():
			if 0 <= idx < group:
				have[idx] = True
		try:
			missing_idx = have.index(False)
		except ValueError:
			return None
		
		if missing_idx == group - 1 and self.fec_last_wait_ms > 0:
			time.sleep(min(0.030, max(0.0, self.fec_last_wait_ms / 1000.0)))

		rec = parity
		for payload in block['chunks'].values():
			rec = self._xor_bytes(rec, payload)

		block['recovered_idx'] = missing_idx
		block['chunks'][missing_idx] = rec
		block['recovered_at'] = time.monotonic()
		# Log successful recovery
		self.fec_recovered_packets += 1 # Global stat
		print(f"[FEC] Recovered 1 missing packet in block {block_id} (group={group}, present={present}, len={len(rec)})")
		return (missing_idx, rec)

	def fecFlushBlock(self, block_id):
		block = self.fec_blocks.get(block_id)
		if not block or block.get('flushed'):
			return

		group = block.get('group_size')
		if not group:
			return
		
		self.fecRecoverPacket(block_id)

		if len(block['chunks']) < group:
			# Packet loss, cannot flush now
			return
		
		# Packet group is enough, flush by index order.
		for idx in range(group):
			chunk = block['chunks'].get(idx)
			if chunk is None:
				continue
			self.pushDataFragment(chunk, marker=0)

		block['flushed'] = True
		del self.fec_blocks[block_id]

	def sendProfile(self, profile, height):
		"""
		profile: "HIGH" | "MED" | "LOW"
		height: int or 0 (original)
		"""
		if self.sessionId == 0:
			return
		
		self.rtspSeq += 1
		desired = 0 if height is None else int(height)
		request = (
			f"RESOLUTION {self.fileName} RTSP/1.0\n"
			f"CSeq: {self.rtspSeq}\n"
			f"Session: {self.sessionId}\n"
			f"X-Resolution: {desired}\n"
			f"X-Profile: {profile}\n\n"
		)
		try:
			self.rtspSocket.send(request.encode())
			print(f"[Client] Sent profile={profile}, height={desired or 'Original'} CSeq={self.rtspSeq}")
			self.requestSent = self.RESOLUTION
		except Exception as e:
			print(f"[Client] Failed to send profile RESOLUTION: {e}")

	def adaptationLoop(self):
		"""Periodic network health check and bitrate adaptation."""
		print("[Client] Adaptation thread started.")

		last_rx_expected = self.rx_expected
		last_rx_lost = self.rx_lost_seq
		last_fec = self.fec_recovered_packets
		last_underruns = self.buffer_underruns

		good_count = 0
		bad_count = 0

		while not self.adapt_stop.is_set() and self.teardownAcked == 0:
			time.sleep(3.0)

			exp = self.rx_expected
			lost = self.rx_lost_seq
			fec = self.fec_recovered_packets
			und = self.buffer_underruns

			d_exp = max(0, exp - last_rx_expected)
			d_lost = max(0, lost - last_rx_lost)
			d_fec = max(0, fec - last_fec)
			d_und = max(0, und - last_underruns)

			last_rx_expected = exp
			last_rx_lost = lost
			last_fec = fec
			last_underruns = und

			if d_exp == 0:
				continue

			loss_rate = d_lost / d_exp
			recovery_rate = d_fec / d_exp

			print(f"[Adapt] window: loss={loss_rate*100:.2f}% "
		 		  f"recovery={recovery_rate*100:.2f}% underrun={d_und} profile={self.net_profile}")
			
			bad = (loss_rate > 0.05) or (d_und > 0)
			good = (loss_rate < 0.01) and (d_und == 0)

			if bad:
				bad_count += 2
				good_count = 0
			elif good:
				good_count += 1
				bad_count = 0
			else:
				bad_count = max(0, bad_count - 1)
				good_count = max(0, good_count - 1)

			if bad_count >= 1:
				self.adaptDown()
				bad_count = 0
			elif good_count >= 3:
				self.adaptUp()
				good_count = 0

	def adaptDown(self):
		old = self.net_profile
		if old == "HIGH":
			self.net_profile = "MED"
			height = self.res_presets.get("480p", 480)
		elif old == "MED":
			self.net_profile = "LOW"
			height = self.res_presets.get("360p", 360)
		else:
			return
		
		print(f"[Adapt] Network degraded; switching profile {old} -> {self.net_profile}")
		try:
			self.sendProfile(self.net_profile, height)
			self.setResolutionUI({ "HIGH": "Original", "MED": "480p", "LOW": "360p" }[self.net_profile])
		except Exception as e:
			print(f"[Adapt] adaptDown sendProfile error: {e}")
		
	def adaptUp(self):
		old = self.net_profile
		if old == "LOW":
			self.net_profile = "MED"
			height = self.res_presets.get("480p", 480)
		elif old == "MED":
			self.net_profile = "HIGH"
			height = self.res_presets.get("Original", None)
		else:
			return
		
		print(f"[Adapt] Network improved; switching profile {old} -> {self.net_profile}")
		try:
			self.sendProfile(self.net_profile, height)
			self.setResolutionUI({ "HIGH": "Original", "MED": "480p", "LOW": "360p" }[self.net_profile])
		except Exception as e:
			print(f"[Adapt] adaptUp sendProfile error: {e}")