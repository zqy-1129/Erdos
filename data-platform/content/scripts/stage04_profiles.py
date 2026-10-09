"""Compatibility entry point for source-bound CUMCM profiles; writes a fresh snapshot."""
from profiles_v3 import main
if __name__=='__main__':
 import sys
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as e:print(str(e),file=sys.stderr);sys.exit(1)
