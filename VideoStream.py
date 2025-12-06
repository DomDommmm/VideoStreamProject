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
        self.total_frames = self._compute_total_frames()   

    def close(self):
        try:
            self.file.close()
        except Exception:
            pass

    def seek_fraction(self, fraction):
        """
        Seek the MJPEG stream to approximately 'fraction' of its length.

        fraction: float in [0.0, 1.0]
        Returns:
            - bytes of the frame at that position, or
            - None if seek fails/ EOF.

        After this call:
            - frameNbr() reflects the new position.
            - subsequent nextFrame() calls continue from this frame.
        """
        if not hasattr(self, 'total_frames') or self.total_frames == 0:
            if self.debug:
                print("[VideoStream] seek_fraction: total_frames unknown or zero, cannot seek.")
            return None
        
        try:
            frac = float(fraction)
        except (TypeError, ValueError):
            if self.debug:
                print(f"[VideoStream] seek_fraction: invalid fraction {fraction}")
            return None
        
        if frac < 0.0:
            frac = 0.0
        elif frac > 1.0:
            frac = 1.0

        if self.total_frames == 1:
            target_idx = 0
        else:
            target_idx = int(frac * (self.total_frames - 1))

        if self.debug:
            print(f"[VideoStream] seek_fraction({fraction}) -> target_idx={target_idx}) "
                  f"/ total_frames={self.total_frames}")
            
        # Reset file + internal state
        try:
            self.file.seek(0, os.SEEK_SET)
        except (OSError, ValueError):
            # If for some reason seek fails, reopen file
            try:
                self.file.close()
            except Exception:
                pass
            self.file = open(self.filename, "rb")
        self.mode = None
        self._buffer.clear()
        self.frameNum = 0

        for _ in range(target_idx):
            frame = self.nextFrame()
            if frame is None:
                # Reached EOF before target frame
                if self.debug:
                    print(f"[VideoStream] seek_fraction: reached EOF before target frame {target_idx}")
                return None
            
        frame = self.nextFrame()
        self.frameNum = target_idx + (1 if frame else 0)
        if frame is None and self.debug:
            print(f"[VideoStream] seek_fraction: no frame at target index {target_idx}")
        return frame

    def _compute_total_frames(self):
        """Scan the file once (with a separate handle) to count frames."""
        try:
            with open(self.filename, "rb") as f:
                header = f.read(5)
                if not header:
                    return 0
                
                # Try to detect len-mode the same way as nextFrame
                try:
                    int(header.strip())
                    return self._count_len_mode(f, header)
                except Exception:
                    return self._count_marker_mode(f, header)
        except Exception as e:
            if self.debug:
                print(f"[VideoStream] _compute_total_frames error: {e}")
            return 0
        
    def _count_len_mode(self, f, first_header):
        """Count frames in length-prefixed format using the same convention as nextFrame"""
        count = 0
        header = first_header

        while header:
            try:
                framelength = int(header.strip())  
            except Exception:
                break  # malformed length header

            data = f.read(framelength)
            if len(data) < framelength:
                break  # EOF before full frame
            count += 1
            header = f.read(5)
        if self.debug:
            print(f"[VideoStream] _count_len_mode: total_frames={count}")
        return count
        
    def _count_marker_mode(self, f, initial_bytes):
        """Count frames in marker mode using SOI/EOI markers."""
        count = 0
        buf = bytearray(initial_bytes)

        while True:
            # Ensure we have some data
            if not buf:
                chunk = f.read(4096)
                if not chunk:
                    break  
                buf.extend(chunk)

            # Find next SOI
            soi = buf.find(self.SOI)
            if soi == -1:
                # keep trailing 0xFF if present
                buf[:] = buf[-1:] if (buf and buf[-1] == 0xFF) else bytearray()
                chunk = f.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                continue
            if soi > 0:
                del buf[:soi]
            
            # Find EOI after SOI
            eoi = buf.find(self.EOI, 2)
            if eoi == -1:
                chunk = f.read(4096)
                if not chunk:
                    return count
                buf.extend(chunk)
                eoi = buf.find(self.EOI, 2)
            
            # Consume this frame
            count += 1
            del buf[:eoi + 2]
        if self.debug:
            print(f"[VideoStream] _count_marker_mode: total_frames={count}")
        return count

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
