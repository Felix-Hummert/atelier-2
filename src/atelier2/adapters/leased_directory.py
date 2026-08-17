"""Enter exactly the directory a lease attested, never the path it was named by.

A lease is taken in one place and the process starts in another, later. Between
the two there is a window: a peer of the same user can remove the leased
directory and move its own into that path. A launcher that passes the path to
`cwd=` resolves the name a second time and hands the provider whatever stands
there now -- with every durable record still saying the attempt ran where it
leased.

So the identity travels with the launch, and the launcher enters it rather than
the name: open the path once with `O_DIRECTORY | O_NOFOLLOW`, `fstat` the
descriptor, refuse unless it is the device and inode the lease attested, and
start the child through `/proc/self/fd/<descriptor>`. The descriptor is the
same object that was checked, so nothing between the check and the `chdir` can
be swapped.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_ENTRY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class LeasedDirectoryChanged(ValueError):
    """The directory standing at the leased path is not the one that was leased.

    A `ValueError` on purpose: to a launcher this is a malformed launch, and it
    joins the supervision failure every other malformed launch already takes.
    A launcher that crashed here would turn a refusal into an outage.
    """


@contextmanager
def entered_leased_directory(
    working_directory: Path, device: int, inode: int
) -> Iterator[tuple[str, int]]:
    """Hold the leased directory open and name the way in through the descriptor.

    Yields the `cwd` a child is started with and the descriptor it must inherit
    for that path to mean anything. Both belong together: `/proc/self/fd/<n>` is
    resolved by the child, so the descriptor has to survive into it.
    """

    try:
        descriptor = os.open(working_directory, _ENTRY_FLAGS)
    except OSError as error:
        raise LeasedDirectoryChanged(
            f"the leased directory {working_directory} could not be opened as the "
            f"directory it was leased as: {error}"
        ) from error
    try:
        standing = os.fstat(descriptor)
        if (standing.st_dev, standing.st_ino) != (device, inode):
            raise LeasedDirectoryChanged(
                f"the directory at {working_directory} is not the one this attempt "
                "leased: its identity changed between the lease and the launch, so "
                "starting there would run the provider on somebody else's ground"
            )
        yield f"/proc/self/fd/{descriptor}", descriptor
    finally:
        os.close(descriptor)
