import os

class VideoStream:
    """
    Robust MJPEG reader supporting two formats:
    1) Length-prefixed frames: 5 ASCII digits (frame length), followed by that many bytes of JPEG.
    2) Raw concatenated JPEG frames: back-to-back JPEGs delimited by SOI (0xFFD8) and EOI (0xFFD9).
    """

    SOI = b"\xff\xd8"  # Start of Image
    EOI = b"\xff\xd9"  # End of Image

    def __init__(self, filename):
        self.filename = filename
        try:
            self.file = open(filename, "rb")
        except Exception:
            raise IOError(f"Cannot open file {filename}")
        self.frameNum = 0
        self.mode = None  # 'len' or 'marker'
        self._buffer = bytearray()
        # optional: toggle debug prints
        self.debug = False

    def close(self):
        try:
            self.file.close()
        except Exception:
            pass

    def _read_more(self, size=4096):
        """Read more bytes into internal buffer (marker mode) or return chunk."""
        chunk = self.file.read(size)
        if chunk:
            self._buffer.extend(chunk)
            if self.debug:
                print(f"[VideoStream] _read_more: read {len(chunk)} bytes, buffer={len(self._buffer)}")
        return chunk

    def _read_exact(self, n):
        """Read exactly n bytes from file or return None if EOF before n."""
        parts = []
        remaining = n
        while remaining > 0:
            chunk = self.file.read(remaining)
            if not chunk:
                # EOF before full read
                return None
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def _next_frame_marker_mode(self):
        """Extract next JPEG frame using SOI/EOI markers from internal buffer."""
        # Make sure buffer has some data
        if not self._buffer:
            if not self._read_more():
                return None

        while True:
            soi_idx = self._buffer.find(self.SOI)
            if soi_idx == -1:
                # No SOI yet: discard data and read more
                # But be careful: don't discard possible trailing 0xFF that could be start of SOI
                # Keep last byte if it's 0xFF
                if len(self._buffer) > 0 and self._buffer[-1] == 0xFF:
                    self._buffer[:] = self._buffer[-1:]
                else:
                    self._buffer.clear()
                if not self._read_more():
                    return None
                continue

            # If SOI found but bytes before SOI are garbage, drop them
            if soi_idx > 0:
                del self._buffer[:soi_idx]

            # Now search for EOI after SOI
            # Start searching from position 2 (after SOI)
            eoi_search_start = 2
            eoi_idx = self._buffer.find(self.EOI, eoi_search_start)
            while eoi_idx == -1:
                # Need more data
                if not self._read_more():
                    # EOF without EOI: no complete frame
                    return None
                eoi_idx = self._buffer.find(self.EOI, eoi_search_start)

            # Extract frame from 0 .. eoi_idx+2
            frame = bytes(self._buffer[: eoi_idx + 2])
            # Remove consumed bytes from buffer
            del self._buffer[: eoi_idx + 2]
            self.frameNum += 1
            if self.debug:
                print(f"[VideoStream] marker frame #{self.frameNum} size={len(frame)}")
            return frame

    def nextFrame(self):
        """Get next frame in either supported format."""
        if self.mode is None:
            # Peek first 5 bytes to detect format, but handle short reads
            header = self.file.read(5)
            if not header:
                return None

            # If header looks like a 5-digit integer (length-prefixed), use 'len' mode
            # header may include spaces/newline so use strip()
            try:
                framelength = int(header.strip())
                # switch to len mode
                self.mode = "len"
                # read exact framelength bytes
                data = self._read_exact(framelength)
                if not data:
                    return None
                self.frameNum += 1
                if self.debug:
                    print(f"[VideoStream] len-mode first frame #{self.frameNum} size={len(data)}")
                return data
            except Exception:
                # fallback to marker mode; push header bytes into buffer for marker scanner
                self.mode = "marker"
                self._buffer.extend(header)
                return self._next_frame_marker_mode()

        if self.mode == "len":
            # In len mode, each frame prefixed by 5 ASCII digits
            header = self._read_exact(5)
            if not header:
                return None
            try:
                framelength = int(header.strip())
            except Exception:
                # malformed length header — abort
                return None
            data = self._read_exact(framelength)
            if not data:
                return None
            self.frameNum += 1
            if self.debug:
                print(f"[VideoStream] len-mode frame #{self.frameNum} size={len(data)}")
            return data
        else:
            # marker mode
            return self._next_frame_marker_mode()

    def frameNbr(self):
        """Get frame number (1-based)."""
        return self.frameNum
