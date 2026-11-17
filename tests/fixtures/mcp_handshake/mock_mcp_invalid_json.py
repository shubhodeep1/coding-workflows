#!/usr/bin/env python3
from __future__ import annotations

from mock_server_lib import read_message, write_raw_json


read_message()
write_raw_json(b'{"jsonrpc":"2.0","id":1,"result":')
