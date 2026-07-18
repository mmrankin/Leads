"""Shared FTP uploader for the auction scrapers (Manheim / Adesa / Copart).

ftp.vin10.net runs ProFTPD on port 21 (plain FTP; it does not offer FTPS/SFTP),
so uploads use ftplib in passive mode. Connection details come from the FTP_*
environment variables (.env). Used by each site's scraper.
"""
import os
from ftplib import FTP, error_perm


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError("missing required env var %s" % name)
    return v


def _connect():
    host = _env("FTP_HOST", required=True)
    port = int(_env("FTP_PORT", "21"))
    user = _env("FTP_USER", required=True)
    password = _env("FTP_PASS", required=True)
    ftp = FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, password)
    ftp.set_pasv(True)
    return ftp


def _ensure_dir(ftp, remote_dir):
    """cwd into remote_dir, creating any missing path segments (mkdir -p)."""
    parts = [p for p in remote_dir.split("/") if p]
    path = ""
    for p in parts:
        path = path + "/" + p
        try:
            ftp.cwd(path)
        except error_perm:
            try:
                ftp.mkd(path)
                ftp.cwd(path)
            except error_perm:
                pass


def upload_files(local_files, remote_dir=None):
    """Upload each path in `local_files` to `remote_dir` on the FTP host (default
    from MANHEIM_REMOTE_DIR). Returns the list of remote paths written."""
    remote_dir = remote_dir or _env("MANHEIM_REMOTE_DIR", required=True)
    ftp = _connect()
    written = []
    try:
        _ensure_dir(ftp, remote_dir)  # leaves cwd at remote_dir
        for lf in local_files:
            name = os.path.basename(lf)
            with open(lf, "rb") as fh:
                ftp.storbinary("STOR " + name, fh)
            written.append(remote_dir.rstrip("/") + "/" + name)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return written


def check_connection(remote_dir=None):
    """Connect, list the target directory, and return (ok, message) — validates
    FTP credentials/reachability without uploading anything."""
    remote_dir = remote_dir or _env("MANHEIM_REMOTE_DIR", required=True)
    try:
        ftp = _connect()
        try:
            _ensure_dir(ftp, remote_dir)
            listing = ftp.nlst()
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        return True, "connected; %s has %d entries" % (remote_dir, len(listing))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
