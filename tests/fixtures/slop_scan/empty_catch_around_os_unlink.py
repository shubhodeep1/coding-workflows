import os


def remove_temp_file(path):
	try:
		os.unlink(path)
	except:
		pass
