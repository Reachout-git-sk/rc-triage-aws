import io

from triage.tail import read_tail, read_tail_from


class CountingReader(io.BytesIO):
    """Records how many bytes were actually read."""

    bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def test_small_file_is_returned_whole(tmp_path):
    path = tmp_path / "small.out"
    path.write_bytes(b"line 1\nline 2\nModuleNotFoundError: No module named 'torch'\n")
    assert read_tail(path) == "line 1\nline 2\nModuleNotFoundError: No module named 'torch'\n"


def test_big_log_reads_only_the_end():
    body = b"".join(b"step %d loss=0.123\n" % i for i in range(300_000))  # ~6 MB
    fh = CountingReader(body + b"RuntimeError: CUDA out of memory.\n")
    text = read_tail_from(fh, max_bytes=4096)
    assert fh.bytes_read <= 4096
    assert text.endswith("RuntimeError: CUDA out of memory.\n")
    assert text.startswith("step ")  # the cut-off first line was dropped


def test_missing_file_returns_none(tmp_path):
    assert read_tail(tmp_path / "nope.out") is None


def test_crlf_ansi_and_bad_bytes_are_cleaned():
    fh = io.BytesIO(b"\x1b[31mERROR\x1b[0m: boom\r\nbad byte \xff here\r\n")
    text = read_tail_from(fh)
    assert text == "ERROR: boom\nbad byte � here\n"
