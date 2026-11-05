# Overrides PyInstaller's contrib hook-webrtcvad.py, which does copy_metadata('webrtcvad') and FAILS THE
# WHOLE BUILD here: we install the maintained fork, published as 'webrtcvad-wheels' (same `webrtcvad`
# module). Its webrtcvad.py reads its version from 'webrtcvad-wheels' metadata (else "unknown").
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata('webrtcvad-wheels')
