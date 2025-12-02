from random import randint
import sys, traceback, threading, socket
import re
import time
import errno
from PIL import Image
import io

from VideoStream import VideoStream
from RtpPacket import RtpPacket

class ServerWorker:
	SETUP = 'SETUP'
	PLAY = 'PLAY'
	PAUSE = 'PAUSE'
	TEARDOWN = 'TEARDOWN'
	RESOLUTION = 'RESOLUTION'
	
	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT

	OK_200 = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500 = 2

	FEC_GROUP_SIZE = 10
	RTP_PT_MJPEG = 26
	RTP_PT_FEC = 127
	DATA_FEC_HEADER_SIZE = 8  # 4 bytes block_id + 2 bytes group_size + 2 bytes fec_len
	
	clientInfo = {}
	
	def __init__(self, clientInfo):
		self.clientInfo = clientInfo
		# Initialize RTP sequence number counter (increments per RTP packet)
		self.seqnum = 0
		self.server_target_height = None # None = Original
		self.jpeg_quality = 65
		self.jpeg_subsampling = "4:2:0"
		self.rtp_debug = True 		# set False to silence per-packet logs
		self.developerMode = False  # Enable DeveloperMode FEC testing
		# Small delay before sending FEC parity to mitigate UDP reordering
		# Parity may arrive earlier than last data fragment on some stacks.
		# Delay is in milliseconds; keep tiny to minimize added latency.
		self.parity_delay_ms = 6
		# Internal plan of which packet to drop per FEC block when developerMode is enabled.
		# Key: block_id -> {"type": "data"|"parity", "index": int}
		# Example: {"type": "data", "index": 3} means drop data fragment #3 of the 10;
		# {"type": "parity"} means drop the parity packet for that block.
		self._dev_drop_plan = {}

	@staticmethod
	def _fec_make_header(block_id, group_size, fec_len):
		# 8 bytes: [block_id(4)][group_size(2)][fec_len(2)]
		return (block_id.to_bytes(4, 'big') +
		  		group_size.to_bytes(2, 'big') + 
				fec_len.to_bytes(2, 'big'))
	
	@staticmethod
	def _xor_bytes(a, b):
		"""XOR two byte strings, padding shorter one with zeros"""
		L = max(len(a), len(b))
		if len(a) < L:
			a = a + b'\x00' * (L - len(a))
		if len(b) < L:
			b = b + b'\x00' * (L - len(b))
		return bytes(x ^ y for x, y in zip(a, b))

	@staticmethod
	def downscale_and_reencode_jpeg(jpeg_bytes, target_height=None, quality=65, subsampling="4:2:0"):
		"""
		Downscale to target_height if provided (no upscaling), then re-encode JPEG with given quality/subsampling. 
		Returns new JPEG bytes. On error, returns original bytes.
		"""
		try:
			img = Image.open(io.BytesIO(jpeg_bytes))
			src_w, src_h = img.size
			target_h = src_h if target_height is None else min(target_height, src_h)

			# Resize if needed (preserve aspect)
			if target_h != src_h:
				aspect = src_w / src_h if src_h else 1.0
				target_w = max(1, int(round(target_h * aspect)))
				img = img.resize((target_w, target_h), Image.BILINEAR)

			subsampling_map = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}
			ss = subsampling_map.get(subsampling, 2)
			out = io.BytesIO()
			img.save(out, format="JPEG", quality=quality, subsampling=ss, optimize=True)
			return out.getvalue()
		except Exception:
			return jpeg_bytes
		
	@staticmethod
	def choose_server_resolution(label):
		"""Map preset labels to heights; None for original."""
		presets = {
			"Original": None,
			"144p": 144,
			"240p": 240,
			"360p": 360,
			"480p": 480,
			"720p": 720,
			"1080p": 1080,
		}
		return presets.get(label, None)
		
	def run(self):
		threading.Thread(target=self.recvRtspRequest).start()
	
	def recvRtspRequest(self):
		"""Receive RTSP request from the client."""
		connSocket = self.clientInfo['rtspSocket'][0]
		# print(connSocket)
		while True:			
			try:
				data = connSocket.recv(1024)
			except ConnectionResetError:
				if self.rtp_debug:
					print("[RTSP Server] Connection reset by client; closing session")
				break
			except Exception as e:
				if self.rtp_debug:
					print(f"[RTSP Server] Error receiving RTSP data: {e}")
				break
			if not data:
				if self.rtp_debug:
					print("[RTSP Server] Client closed RTSP connection")

				# Stop sender thread if running
				evt = self.clientInfo.get('event')
				if evt:
					evt.set()

				worker = self.clientInfo.get('worker')
				if worker and worker.is_alive():
					try:
						worker.join(timeout=1.0)
					except Exception:
						pass

				# Close RTP socket after worker exits
				sock = self.clientInfo.get('rtpSocket')
				if sock:
					try:
						sock.close()
					except Exception:
						pass

				# Optionally close the video file
				vs = self.clientInfo.get('videoStream')
				if vs:
					try:
						vs.close()
					except Exception:
						pass

				break
			print("Data received:\n" + data.decode("utf-8"))
			self.processRtspRequest(data.decode("utf-8"))
	
	def processRtspRequest(self, data):
		"""Process RTSP request sent from the client."""
		# Normalize and parse RTSP request
		lines = [ln for ln in data.replace('\r\n', '\n').split('\n') if ln]
		if not lines:
			return
		start_tokens = lines[0].split()
		if not start_tokens:
			return
		# Accept optional leading "C:" token
		if start_tokens[0] in ('C:', 'S:') and len(start_tokens) >= 2:
			start_tokens = start_tokens[1:]
		requestType = start_tokens[0]
		filename = start_tokens[1] if len(start_tokens) > 1 else ''
		# Parse headers into a dict
		headers = {}
		for header_line in lines[1:]:
			if ':' in header_line:
				k, v = header_line.split(':', 1)
				headers[k.strip()] = v.strip()
		seq = headers.get('CSeq', '0')
		
		# Process SETUP request
		if requestType == self.SETUP:
			if self.state == self.INIT:
				# Update state
				print("processing SETUP\n")
				
				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.state = self.READY
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq)
				
				# Generate a randomized RTSP session ID
				self.clientInfo['session'] = randint(100000, 999999)
				
				# Send RTSP reply
				self.replyRtsp(self.OK_200, seq)
				
				# Get the RTP/UDP port from Transport header (Transport: RTP/UDP; client_port=XXXX)
				transport = headers.get('Transport', '')
				self.clientInfo['rtpPort'] = None
				if transport:
					for token in re.split(r'[;\s]', transport):
						if token.startswith('client_port='):
							try:
								self.clientInfo['rtpPort'] = int(token.split('=', 1)[1])
							except ValueError:
								self.clientInfo['rtpPort'] = None
							break
		
		# Process PLAY request 		
		elif requestType == self.PLAY:
			if self.state == self.READY:
				print("processing PLAY\n")
				self.state = self.PLAYING
				
				# Guard: require rtpPort to be set during SETUP
				if not self.clientInfo.get('rtpPort'):
					if self.rtp_debug:
						print("[RTSP Server] PLAY received without valid RTP port; replying 500 and staying READY")
					self.state = self.READY
					self.replyRtsp(self.CON_ERR_500, seq)
					return
				
				# Create a new socket for RTP/UDP
				self.clientInfo["rtpSocket"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
				# Increase send buffer to reduce UDP drops
				try:
					self.clientInfo["rtpSocket"].setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
				except Exception:
					pass
				
				self.replyRtsp(self.OK_200, seq)
				
				# Create a new thread and start sending RTP packets
				self.clientInfo['event'] = threading.Event()
				self.clientInfo['worker'] = threading.Thread(target=self.sendRtp, daemon=True)
				self.clientInfo['worker'].start()
		
		# Process PAUSE request
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				print("processing PAUSE\n")
				self.state = self.READY
				
				self.clientInfo['event'].set()
			
				self.replyRtsp(self.OK_200, seq)
		
		# Process RESOLUTION request
		elif requestType == self.RESOLUTION:
			# Accept resolution changes at any time
			# Update current target height
			desired = headers.get('X-Resolution', '0')
			try:
				desired_h = int(desired)
			except Exception:
				desired_h = 0
			self.server_target_height = None if desired_h == 0 else desired_h
			if self.rtp_debug:
				print(f"[RTSP Server] RESOLUTION {desired_h or 'Original'} accepted.")
			self.replyRtsp(self.OK_200, seq)

		# Process TEARDOWN request
		elif requestType == self.TEARDOWN:
			print("processing TEARDOWN\n")

			if 'event' in self.clientInfo:
				self.clientInfo['event'].set()
			
			self.replyRtsp(self.OK_200, seq)

			# Wait for sender thread to exit cleanly
			worker = self.clientInfo.get('worker')
			if worker and worker.is_alive():
				try:
					worker.join(timeout=1.0)
				except Exception:
					pass
			
			sock = self.clientInfo.get('rtpSocket')
			if sock:
				try:
					sock.close()
				except Exception:
					pass
			
			vs = self.clientInfo.get('videoStream')
			if vs:
				try:
					vs.close()
				except Exception:
					pass
			
	def sendRtp(self):
		"""Send RTP packets over UDP."""
		MAX_PAYLOAD = 1100
		rtpSocket = self.clientInfo['rtpSocket']
		address = self.clientInfo['rtspSocket'][1][0]
		port = int(self.clientInfo['rtpPort'])
		print(f"[RTP] Sender started to {address}:{port} (MAX_PAYLOAD={MAX_PAYLOAD})")

		# DeveloperMode utilities
		try:
			import random
		except Exception:
			random = None

		target_fps = 30.0
		frame_interval = 1.0 / target_fps

		block_id = 0
		fec_block_payloads = []       # list of payload bytes for current block
		fec_block_seqnums = []		  # RTP seqnums in the block (for logging)

		while True:
			# Stop if PAUSE or TEARDOWN signaled
			if self.clientInfo['event'].isSet():
				break

			frame_start = time.monotonic()

			data = self.clientInfo['videoStream'].nextFrame()
			if not data:
				# Before exiting, if a partially filled block exists with >= 1 data, send parity anyway
				if fec_block_payloads:
					self._send_fec_parity(rtpSocket, address, port, block_id, fec_block_payloads)
					fec_block_payloads.clear()
					fec_block_seqnums.clear()
					block_id += 1
				break

			# Downscale + re-encode per current server_target_height
			try:
				compressed = self.downscale_and_reencode_jpeg(
					data,
					target_height=self.server_target_height,
					quality=self.jpeg_quality,
					subsampling=self.jpeg_subsampling
				)
			except Exception:
				compressed = data

			# Fragment the (possibly compressed) frame and send RTP + collect for FEC
			total_len = len(compressed)
			offset = 0
			n_frags = (total_len + MAX_PAYLOAD - 1)	// MAX_PAYLOAD
			# Spread fragments over frame_interval
			# If there are many fragments, this maybe sub-ms; Python granularity isn't perfect, but it helps a lot.
			per_frag_gap = frame_interval / max(n_frags, 1)
			next_send_time = frame_start # Schedule first fragment at frame_start
			frame_first_seq = self.seqnum + 1  # predicted first seq of this frame

			frag_idx = 0
			while offset < total_len:
				# Early exit if paused/ teardown mid-frame
				if self.clientInfo['event'].isSet():
					break

				chunk = compressed[offset:offset + MAX_PAYLOAD]
				offset += len(chunk)
				
				# Pace this segment
				now = time.monotonic()
				delay = next_send_time - now
				if delay > 0:
					time.sleep(min(delay, 0.002))
				else:
					next_send_time = now
				
				self.seqnum += 1
				marker = 1 if offset >= total_len else 0

				index_in_block = len(fec_block_payloads)
				data_header = (
					block_id.to_bytes(4, 'big') +
					index_in_block.to_bytes(2, 'big') +
					self.FEC_GROUP_SIZE.to_bytes(2, 'big')
				)
				mjpeg_payload = data_header + chunk
				packet = self.makeRtp(mjpeg_payload, self.seqnum, marker, payload_type=self.RTP_PT_MJPEG)
				# DeveloperMode: plan a drop at the start of a block to simulate a packet loss
				if self.developerMode and random is not None and index_in_block == 0 and (block_id not in self._dev_drop_plan):
					# 20% chance to enable test mode for this block
					if random.random() < 0.05:
						if random.random() < 0.5:
							idx = randint(0, self.FEC_GROUP_SIZE - 1)
							self._dev_drop_plan[block_id] = {"type": "data", "index": idx}
							if self.rtp_debug:
								print(f"[DEV] Planned drop: data idx={idx} for block {block_id}")
						else:
							self._dev_drop_plan[block_id] = {"type": "parity", "index": -1}
							if self.rtp_debug:
								print(f"[DEV] Planned drop: parity for block {block_id}")

				# Should we drop this data packet per plan?
				drop_data = False
				plan = self._dev_drop_plan.get(block_id)
				if self.developerMode and plan and plan.get("type") == "data" and plan.get("index") == index_in_block:
					drop_data = True
				try:
					if not drop_data:
						rtpSocket.sendto(packet, (address, port))
						# if self.rtp_debug:
							# print(f"[RTP] pkt seq={self.seqnum} frame={self.clientInfo['videoStream'].frameNbr() + (1 if marker else 0)} "
							    #   f"frag={frag_idx + 1}/{n_frags} size={len(chunk)} bytes marker={marker}")
						if marker:
							preset = self.server_target_height or 'Original'
							print(f"[RTP] Frame #{self.clientInfo['videoStream'].frameNbr()} complete seq={frame_first_seq}-{self.seqnum} "
							      f"totalBytes={total_len} frags={n_frags} preset={preset} q={self.jpeg_quality}")
					else:
						if self.rtp_debug:
							print(f"[DEV] Dropped data fragment idx={len(fec_block_payloads)} in block {block_id}")
				except OSError as e:
					# Socket closed during teardown — exit quietly
					if e.errno in (errno.EBADF, errno.ENOTSOCK, errno.ENOTCONN):
						return
					print(f"Connection Error during RTP send: {e}")
					return

				# Collect payload and seq for FEC parity computation (even if dropped)
				fec_block_payloads.append(chunk)
				fec_block_seqnums.append(self.seqnum)
				
				# If we have FEC_GROUP_SIZE data packets, send parity now and start next block
				if len(fec_block_payloads) >= self.FEC_GROUP_SIZE:
					self._send_fec_parity(rtpSocket, address, port, block_id, fec_block_payloads)
					fec_block_payloads.clear()
					fec_block_seqnums.clear()
					block_id += 1

				frag_idx += 1
				next_send_time += per_frag_gap
			
			# pacing to next frame
			elapsed = time.monotonic() - frame_start
			remaining = frame_interval - elapsed
			if remaining > 0:
				end_time = time.monotonic() + remaining
				while True:
					if self.clientInfo['event'].isSet():
						break
					rem = end_time - time.monotonic()
					if rem <= 0:
						break
					time.sleep(min(0.002, rem))

	def _send_fec_parity(self, rtpSocket, address, port, block_id, payload_list):
		"""Compute and send FEC parity packet for the given payloads."""
		# Mitigate reordering: wait a tiny bit so parity is less likely
		# to outrun the last data fragment in this block.
		try:
			if isinstance(self.parity_delay_ms, (int, float)) and self.parity_delay_ms > 0:
				time.sleep(min(0.010, max(0.0, self.parity_delay_ms / 1000.0)))
		except Exception:
			pass
		parity = b'\x00'
		for p in payload_list:
			parity = self._xor_bytes(parity, p)
		header = self._fec_make_header(block_id, len(payload_list), len(parity))
		fec_payload = header + parity
		self.seqnum += 1
		fec_packet = self.makeRtp(fec_payload, self.seqnum, marker=0, payload_type=self.RTP_PT_FEC)
		try:
			# DeveloperMode: possibly drop parity
			plan = self._dev_drop_plan.get(block_id)
			if self.developerMode and plan and plan.get("type") == "parity":
				if self.rtp_debug:
					print(f"[DEV] Dropped parity for block {block_id}")
				# Clear plan
				del self._dev_drop_plan[block_id]
			else:
				rtpSocket.sendto(fec_packet, (address, port))
				# if self.rtp_debug:
				# 	print(f"[FEC] pkt seq={self.seqnum} block_id={block_id} group_size={len(payload_list)} "
				#   		  f"fec_len={len(parity)} bytes")
				# Clear plan if any
				if plan and block_id in self._dev_drop_plan:
					del self._dev_drop_plan[block_id]
		except OSError as e:
			if e.errno in (errno.EBADF, errno.ENOTSOCK, errno.ENOTCONN):
				return
			print(f"Connection Error during FEC RTP send: {e}")

	def makeRtp(self, payload, seqnum, marker, payload_type=RTP_PT_MJPEG):
		"""RTP-packetize the video data."""
		version = 2
		padding = 0
		extension = 0
		cc = 0
		pt = payload_type
		ssrc = 0

		rtpPacket = RtpPacket()
		rtpPacket.encode(version, padding, extension, cc, seqnum, marker, pt, ssrc, payload)		
		return rtpPacket.getPacket()
		
	def replyRtsp(self, code, seq):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = (
				f"RTSP/1.0 200 OK\n"
				f"CSeq: {seq}\n"
				f"Session: {self.clientInfo['session']}\n\n"  # Blank line terminator per RTSP RFC
			)
			try:
				connSocket = self.clientInfo['rtspSocket'][0]
				connSocket.send(reply.encode())
			except Exception as e:
				print(f"[RTSP Server] Failed to send 200 reply: {e}")
		
		# Error messages
		elif code == self.FILE_NOT_FOUND_404:
			print("404 NOT FOUND")
			reply = (
				f"RTSP/1.0 404 NOT FOUND\n"
				f"CSeq: {seq}\n"
				f"Session: {self.clientInfo.get('session', 0)}\n\n"
			)
			try:
				connSocket = self.clientInfo['rtspSocket'][0]
				connSocket.send(reply.encode())
			except Exception as e:
				print(f"[RTSP Server] Failed to send 404 reply: {e}")
		elif code == self.CON_ERR_500:
			print("500 CONNECTION ERROR")
			reply = (
				f"RTSP/1.0 500 INTERNAL ERROR\n"
				f"CSeq: {seq}\n"
				f"Session: {self.clientInfo.get('session', 0)}\n\n"
			)
			try:
				connSocket = self.clientInfo['rtspSocket'][0]
				connSocket.send(reply.encode())
			except Exception as e:
				print(f"[RTSP Server] Failed to send 500 reply: {e}")
