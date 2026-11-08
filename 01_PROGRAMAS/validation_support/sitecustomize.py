"""Hermetic process guard automatically loaded by Python in TMAD validation mode."""
import os
if str(os.getenv('TMAD_VALIDATION_MODE','0')).lower() in {'1','true','yes','on'}:
    for key in list(os.environ):
        if 'WEBHOOK' in key.upper() or key.upper().startswith('DISCORD_'):
            os.environ[key]=''
    import socket
    class ValidationNetworkBlocked(RuntimeError): pass
    def _blocked(*args, **kwargs):
        raise ValidationNetworkBlocked('TMAD validation mode blocks all network access')
    socket.create_connection=_blocked
    _orig_socket=socket.socket
    class GuardedSocket(_orig_socket):
        def connect(self, *args, **kwargs): return _blocked(*args, **kwargs)
        def connect_ex(self, *args, **kwargs): return _blocked(*args, **kwargs)
    socket.socket=GuardedSocket
