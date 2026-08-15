import os
import selectors
import signal
import subprocess
import time


class BoundedProcessFailure(OSError): ...


def bounded_process_answer(
    process: subprocess.Popen[bytes], timeout_seconds: float, maximum_output_bytes: int
) -> tuple[int, bytes]:
    if process.stdout is None or process.stderr is None:
        raise BoundedProcessFailure("bounded process has no readable streams")
    streams = (process.stdout, process.stderr)
    deadline = time.monotonic() + timeout_seconds
    standard_output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            for stream, output in zip(streams, (standard_output, bytearray())):
                descriptor = stream.fileno()
                os.set_blocking(descriptor, False)
                selector.register(descriptor, selectors.EVENT_READ, output)
            while selector.get_map():
                try:
                    ready_streams = selector.select(max(0, deadline - time.monotonic()))
                except OverflowError as error:
                    raise BoundedProcessFailure("process deadline failed") from error
                if not ready_streams:
                    raise BoundedProcessFailure("process did not answer in time")
                for ready, _events in ready_streams:
                    output = ready.data
                    chunk = os.read(ready.fd, maximum_output_bytes + 1 - len(output))
                    if not chunk:
                        selector.unregister(ready.fd)
                        continue
                    output += chunk
                    if len(output) > maximum_output_bytes:
                        raise BoundedProcessFailure(
                            f"bounded process answered with more than {maximum_output_bytes} bytes"
                        )
        try:
            return_code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise BoundedProcessFailure(
                "bounded process did not answer in time"
            ) from error
        return return_code, bytes(standard_output)
    finally:
        _reap_process(process, deadline)


def _reap_process(process: subprocess.Popen[bytes], deadline: float) -> None:
    try:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise BoundedProcessFailure("bounded process remained") from error
                raise
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise BoundedProcessFailure("bounded process tree remained") from error
    finally:
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
