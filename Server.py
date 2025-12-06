import socket, argparse

from ServerWorker import ServerWorker


def _str2bool(value):
	"""Parse common true/false strings for CLI flags."""
	if isinstance(value, bool):
		return value
	val = str(value).lower()
	if val in ("yes", "true", "t", "1", "on"):
		return True
	if val in ("no", "false", "f", "0", "off"):
		return False
	raise argparse.ArgumentTypeError("expected a boolean value")


class Server:	
	def main(self):
		parser = argparse.ArgumentParser(description="RTSP video server")
		parser.add_argument("server_port", type=int, help="Port to listen for RTSP connections")
		parser.add_argument(
			"--developerMode", "--developer-mode",
			dest="developer_mode",
			type=_str2bool,
			nargs="?",
			const=True,
			default=False,
			help="Enable developer FEC drop testing (default: false)",
		)
		parser.add_argument(
			"--rtpDebug", "--rtp-debug",
			dest="rtp_debug",
			type=_str2bool,
			nargs="?",
			const=True,
			default=False,
			help="Verbose RTP logging (default: false, set true to enable)",
		)
		args = parser.parse_args()

		SERVER_PORT = args.server_port
		developerMode = args.developer_mode
		rtpDebug = args.rtp_debug

		rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		rtspSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		rtspSocket.bind(('0.0.0.0', SERVER_PORT))
		rtspSocket.listen(5)        

		print(f"[RTSP Server] Listening on 0.0.0.0:{SERVER_PORT} (rtpDebug={rtpDebug}, developerMode={developerMode})")

		# Receive client info (address,port) through RTSP/TCP session
		while True:
			clientInfo = {}
			clientInfo['rtspSocket'] = rtspSocket.accept()
			print(f"[RTSP Server] Accepted connection from {clientInfo['rtspSocket'][1]}")
			ServerWorker(clientInfo, developerMode=developerMode, rtpDebug=rtpDebug).run()		

if __name__ == "__main__":
	(Server()).main()
