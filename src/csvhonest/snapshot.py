"""Private disk-backed input captures for consistent multi-pass verification."""

import os
import shutil
import tempfile
from contextlib import contextmanager


@contextmanager
def captured_path(path, enabled=True):
    """Yield a private capture, deleting it on success or failure.

    A concurrent writer can still affect bytes while they are being copied.
    This is not a filesystem transaction. Once copied, however, every pass
    reads the same captured bytes rather than a moving source pathname.
    Temporary disk use is proportional to the complete input size.
    """
    if not enabled:
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="csvhonest-") as directory:
        # Keep the basename so loaders retain their normal suffix inference.
        destination = os.path.join(directory, os.path.basename(path))
        shutil.copyfile(path, destination)
        yield destination
