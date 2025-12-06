import argparse
import sys
from tkinter import Tk
from Client import Client

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

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="RTP Client Launcher")
	parser.add_argument("server_addr")
	parser.add_argument("server_port")
	parser.add_argument("rtp_port")
	parser.add_argument("video_file")
	parser.add_argument(
		"--rtpDebug", "--rtp-debug",
		dest="rtp_debug",
		type=_str2bool,
		nargs="?",
		const=True,
		default=False,
		help="Verbose RTP logging (default: true, set false to quiet)",
	)
	args = parser.parse_args()

	root = Tk()
	app = Client(
		root,
		args.server_addr,
		args.server_port,
		args.rtp_port,
		args.video_file,
		debug=args.rtp_debug,
	)
	app.master.title("RTPClient")
	root.mainloop()
	print("[Usage: ClientLauncher.py Server_name Server_port RTP_port Video_file [Debug]]\n")	
	