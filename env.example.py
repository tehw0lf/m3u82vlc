import os

base_path = f"{os.path.expanduser('~')}/Downloads"
favorites = ["https://mystreamingsite.com/nyancat"]
elements_to_click_on_load = ["some_element_id"]

# remote control (server.py)
server_host = "127.0.0.1"  # the LAN address of this machine to allow phones
server_port = 8338
server_token = "change-me"  # generated on every start if not set
