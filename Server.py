import sys, socket

from ServerWorker import ServerWorker

class Server:	
	
	def main(self):
		try:
			SERVER_PORT = int(sys.argv[1])
		except:
			print("[Usage: Server.py Server_port]\n")
		rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		rtspSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		rtspSocket.bind(('0.0.0.0', SERVER_PORT))
		rtspSocket.listen(5)        

		print(f"[RTSP Server] Listening on 0.0.0.0:{SERVER_PORT}")

		# Receive client info (address,port) through RTSP/TCP session
		while True:
			clientInfo = {}
			clientInfo['rtspSocket'] = rtspSocket.accept()
			print(f"[RTSP Server] Accepted connection from {clientInfo['rtspSocket'][1]}")
			ServerWorker(clientInfo).run()		

if __name__ == "__main__":
	(Server()).main()