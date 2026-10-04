import os

base_path = f"{os.path.expanduser('~')}/Downloads"
favorites = ["https://mystreamingsite.com/nyancat"]
elements_to_click_on_load = ["some_element_id"]

# remote control (server.py)
server_host = "127.0.0.1"  # the LAN address of this machine to allow phones
server_port = 8338
# server_token = "..."  # generated on every start if not set
# serve HTTPS instead of HTTP, see the README for creating a certificate
# server_cert = f"{os.path.expanduser('~')}/.config/m3u82vlc/cert.pem"
# server_key = f"{os.path.expanduser('~')}/.config/m3u82vlc/key.pem"
