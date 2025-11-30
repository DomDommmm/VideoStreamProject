import sys
from tkinter import Tk, Toplevel
from Client import Client

"""Launch multiple Client windows for different video files.
Usage:
    python MultiClientLauncher.py <server_addr> <server_port> <rtp_port_start> <video1> [<video2> ...]
Each client gets an incremented RTP port starting at rtp_port_start.
"""

def main():
    if len(sys.argv) < 5:
        print("Usage: MultiClientLauncher.py <server_addr> <server_port> <rtp_port_start> <video1> [<video2> ...]")
        sys.exit(1)
    server_addr = sys.argv[1]
    server_port = sys.argv[2]
    rtp_start = int(sys.argv[3])
    videos = sys.argv[4:]

    root = Tk()
    root.withdraw()  # Hide the root; use Toplevels for each client

    for i, vid in enumerate(videos):
        win = Toplevel(root)
        win.title(f"RTPClient - {vid}")
        rtp_port = rtp_start + i
        Client(win, server_addr, server_port, rtp_port, vid)

    root.mainloop()

if __name__ == "__main__":
    main()
