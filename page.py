INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>m3u82vlc</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0 auto; padding: 16px; max-width: 640px;
    background: #16161a; color: #e8e8ea;
    font: 16px/1.4 system-ui, sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 16px; }
  h2 { font-size: 14px; margin: 24px 0 8px; color: #9a9aa5;
       text-transform: uppercase; letter-spacing: .05em; }
  form { display: flex; gap: 8px; }
  input {
    flex: 1; min-width: 0; padding: 12px; font: inherit;
    background: #23232a; color: inherit;
    border: 1px solid #3a3a45; border-radius: 8px;
  }
  button {
    padding: 12px 16px; font: inherit; cursor: pointer;
    background: #5b3fc4; color: #fff; border: 0; border-radius: 8px;
  }
  button.quiet { background: #2e2e38; }
  button.danger { background: #a83232; }
  button:disabled { opacity: .4; cursor: default; }
  ul { list-style: none; margin: 0; padding: 0; }
  li {
    display: flex; align-items: center; gap: 12px; padding: 12px;
    margin-bottom: 8px; background: #23232a; border-radius: 8px;
  }
  .light {
    flex: none; width: 14px; height: 14px; border-radius: 50%;
    background: #6b6b76;
  }
  .light.green { background: #35c46a; }
  .light.red { background: #e04a4a; }
  .light.amber { background: #e0a030; animation: pulse 1s infinite; }
  @keyframes pulse { 50% { opacity: .3; } }
  .text { flex: 1; min-width: 0; }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .note { font-size: 13px; color: #9a9aa5; }
  .empty { color: #6b6b76; padding: 4px 0; }
  #message { color: #e04a4a; min-height: 1.4em; margin: 8px 0 0; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<h1>m3u82vlc</h1>

<form id="token-form" hidden>
  <input id="token" type="password" placeholder="Access token"
         autocomplete="off">
  <button>Save</button>
</form>

<div id="app" hidden>
  <form id="link-form">
    <input id="url" type="url" placeholder="Stream URL" required
           autocomplete="off" autocapitalize="off">
    <button>Send</button>
  </form>
  <p id="message"></p>

  <h2>Links</h2>
  <ul id="links"></ul>

  <h2>Recordings</h2>
  <ul id="recordings"></ul>
</div>

<script>
const STATES = {
  waiting: ["", "Waiting"],
  searching: ["amber", "Searching"],
  not_found: ["red", "No stream found"],
  error: ["red", "Error"],
  starting: ["amber", "Starting recording"],
  recording: ["green", "Recording"],
  finished: ["", "Recording finished"],
};

const query = new URLSearchParams(location.search);
if (query.has("token")) {
  localStorage.setItem("token", query.get("token"));
  history.replaceState(null, "", location.pathname);
}

const $ = (id) => document.getElementById(id);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function askForToken() {
  $("app").hidden = true;
  $("token-form").hidden = false;
}

async function api(method, path, body) {
  const response = await fetch("/api" + path, {
    method,
    headers: {
      "Authorization": "Bearer " + (localStorage.getItem("token") || ""),
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401) {
    askForToken();
    throw new Error("Invalid token");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === "string" ? error.detail : "Request failed"
    );
  }
  return response.status === 204 ? null : response.json();
}

async function act(method, path, body) {
  $("message").textContent = "";
  try {
    await api(method, path, body);
  } catch (error) {
    $("message").textContent = error.message;
  }
  refresh();
}

function duration(seconds) {
  const pad = (value) => String(value).padStart(2, "0");
  return Math.floor(seconds / 3600) + ":" + pad(Math.floor(seconds / 60) % 60)
    + ":" + pad(seconds % 60);
}

function row(light, name, note, buttons) {
  const item = element("li");
  item.append(element("span", "light " + light));
  const text = element("div", "text");
  text.append(element("div", "name", name), element("div", "note", note));
  item.append(text, ...buttons);
  return item;
}

function button(label, className, onClick) {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function render(state) {
  const links = state.links.slice().reverse().map((link) => {
    const [light, label] = STATES[link.state] || ["", link.state];
    const remove = button("\\u00d7", "quiet", () =>
      act("DELETE", "/links/" + link.id));
    // A link in use stays in the list until it is done
    remove.disabled = ["searching", "starting", "recording"]
      .includes(link.state);
    const note = link.detail ? label + " \\u00b7 " + link.detail : label;
    return row(light, link.name, note, [remove]);
  });
  $("links").replaceChildren(
    ...(links.length ? links : [element("li", "empty", "No links yet")]));

  const recordings = state.recordings.map((recording) => {
    const size = (recording.size / 1e6).toFixed(1) + " MB";
    const stop = button("Stop", "danger", () => {
      if (confirm("Stop recording " + recording.name + "?")) {
        act("DELETE", "/recordings/" + recording.pid);
      }
    });
    return row("green", recording.name,
      duration(recording.elapsed) + " \\u00b7 " + size, [stop]);
  });
  $("recordings").replaceChildren(...(recordings.length
    ? recordings : [element("li", "empty", "No active recordings")]));
}

async function refresh() {
  try {
    render(await api("GET", "/state"));
    $("token-form").hidden = true;
    $("app").hidden = false;
  } catch (error) {
    // Shown as the token form, or retried on the next refresh
  }
}

$("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  localStorage.setItem("token", $("token").value.trim());
  $("token").value = "";
  refresh();
});

$("link-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const url = $("url").value;
  $("url").value = "";
  act("POST", "/links", { url });
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
