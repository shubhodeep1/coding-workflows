import os


def safe_unlink_quiet_cleanup(path):
	"""Best-effort cleanup helper for temporary files."""
	try:
		os.unlink(path)
	except:
		pass
