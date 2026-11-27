#!/usr/bin/env python3
from __future__ import annotations

from mock_server_lib import read_message, write_message


request = read_message()
write_message(
	{
		"jsonrpc": "2.0",
		"id": request["id"],
		"result": {
			"serverInfo": {
				"name": "mock-serena",
				"version": "0.0.1",
			},
		},
	}
)
