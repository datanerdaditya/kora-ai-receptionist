/*
 * Clinic voice widget.
 *
 * A clinic adds one line to its website:
 *   <script src="https://YOUR-SERVER/widget.js" data-clinic="riviere-dental"></script>
 *
 * Optional attributes on the script tag:
 *   data-label="Talk to us"     text on the button
 *   data-color="#0f766e"        main colour
 *   data-server="https://..."   server address, if different from where this file is hosted
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) return;

  var CLINIC = script.getAttribute("data-clinic");
  var SERVER = (script.getAttribute("data-server") || new URL(script.src).origin).replace(/\/$/, "");
  var LABEL = script.getAttribute("data-label") || "Talk to us";
  var COLOR = script.getAttribute("data-color") || "#0f766e";
  var LIVEKIT_SCRIPT = "https://cdn.jsdelivr.net/npm/livekit-client@2.24.1/dist/livekit-client.umd.js";

  if (!CLINIC) {
    console.error("Clinic widget: the script tag needs a data-clinic attribute.");
    return;
  }

  // What the visitor sees for each state of the call
  var STATES = {
    connecting: { text: "Connecting…", tone: "wait" },
    initializing: { text: "Connecting…", tone: "wait" },
    idle: { text: "Listening", tone: "listen" },
    listening: { text: "Listening", tone: "listen" },
    thinking: { text: "Thinking…", tone: "think" },
    speaking: { text: "Speaking", tone: "speak" },
    ended: { text: "Call ended", tone: "off" },
    error: { text: "Something went wrong", tone: "off" },
  };

  var room = null;
  var micOn = false;
  var bubbles = {}; // transcript bubbles by segment id
  var audioElements = [];
  var livekitLoading = null;

  // ---------- build the interface (inside a shadow root, so the clinic's own
  // styles cannot break it and ours cannot leak out) ----------

  var host = document.createElement("div");
  host.setAttribute("data-clinic-widget", "");
  var root = host.attachShadow({ mode: "open" });

  root.innerHTML =
    "<style>" +
    ":host{all:initial}" +
    "*{box-sizing:border-box;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}" +
    ".launcher{position:fixed;right:20px;bottom:20px;z-index:2147483000;display:flex;align-items:center;gap:10px;" +
    "padding:14px 20px;border:0;border-radius:999px;background:var(--c);color:#fff;font-size:16px;font-weight:600;" +
    "cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.2)}" +
    ".launcher:hover{filter:brightness(1.08)}" +
    ".launcher svg{width:20px;height:20px;fill:currentColor}" +
    ".panel{position:fixed;right:20px;bottom:20px;z-index:2147483001;width:360px;max-width:calc(100vw - 24px);" +
    "height:540px;max-height:calc(100vh - 24px);display:none;flex-direction:column;background:#fff;color:#111827;" +
    "border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.25);overflow:hidden}" +
    ".panel.open{display:flex}" +
    ".head{padding:14px 16px;background:var(--c);color:#fff}" +
    ".title{font-size:16px;font-weight:700;margin:0}" +
    ".sub{font-size:12px;opacity:.9;margin:2px 0 0}" +
    ".status{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid #e5e7eb;font-size:15px;font-weight:600}" +
    ".dot{width:12px;height:12px;border-radius:50%;background:#9ca3af;flex:none}" +
    ".dot.listen{background:#16a34a;animation:pulse 1.6s ease-in-out infinite}" +
    ".dot.think{background:#d97706;animation:pulse .8s ease-in-out infinite}" +
    ".dot.speak{background:var(--c);animation:pulse 1.1s ease-in-out infinite}" +
    ".dot.wait{background:#9ca3af;animation:pulse 1s ease-in-out infinite}" +
    "@keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.5);opacity:.55}}" +
    "@media (prefers-reduced-motion:reduce){.dot{animation:none!important}}" +
    ".log{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:8px;background:#f9fafb}" +
    ".msg{max-width:85%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere}" +
    ".msg.agent{align-self:flex-start;background:#fff;border:1px solid #e5e7eb}" +
    ".msg.user{align-self:flex-end;background:var(--c);color:#fff}" +
    ".note{align-self:center;font-size:12px;color:#6b7280;text-align:center;padding:4px 8px}" +
    ".foot{padding:10px 12px;border-top:1px solid #e5e7eb;display:flex;flex-direction:column;gap:8px;background:#fff}" +
    ".row{display:flex;gap:8px}" +
    "input{flex:1;min-width:0;padding:10px 12px;border:1px solid #d1d5db;border-radius:10px;font-size:14px;color:#111827;background:#fff}" +
    "input:focus,button:focus-visible{outline:2px solid var(--c);outline-offset:2px}" +
    "button.small{padding:10px 14px;border:1px solid #d1d5db;border-radius:10px;background:#fff;color:#111827;font-size:14px;font-weight:600;cursor:pointer}" +
    "button.small:hover{background:#f3f4f6}" +
    "button.small:disabled{opacity:.5;cursor:default}" +
    "button.end{flex:1;border-color:#dc2626;background:#dc2626;color:#fff}" +
    "button.end:hover{background:#b91c1c}" +
    "button.again{flex:1;border-color:var(--c);background:var(--c);color:#fff}" +
    "button.again:hover{background:var(--c);filter:brightness(1.08)}" +
    ".close{position:absolute;top:10px;right:10px;width:30px;height:30px;border:0;border-radius:50%;background:rgba(255,255,255,.2);" +
    "color:#fff;font-size:18px;line-height:1;cursor:pointer}" +
    "</style>" +
    '<button class="launcher" type="button" aria-haspopup="dialog">' +
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V22h2v-3.08A7 7 0 0 0 19 12h-2z"/></svg>' +
    '<span class="launcher-label"></span></button>' +
    '<div class="panel" role="dialog" aria-label="Voice assistant">' +
    '<div class="head"><p class="title"></p><p class="sub">AI assistant. You can speak or type.</p>' +
    '<button class="close" type="button" aria-label="Close">×</button></div>' +
    '<div class="status" role="status" aria-live="polite"><span class="dot"></span><span class="status-text"></span></div>' +
    '<div class="log" aria-live="polite"></div>' +
    '<div class="foot">' +
    '<form class="row"><input type="text" placeholder="Or type your message" aria-label="Type your message" autocomplete="off" maxlength="500">' +
    '<button class="small send" type="submit">Send</button></form>' +
    '<div class="row"><button class="small mic" type="button"></button>' +
    '<button class="small end" type="button">End call</button>' +
    '<button class="small again" type="button" hidden>Start a new call</button></div>' +
    "</div></div>";

  host.style.setProperty("--c", COLOR);

  var el = {
    launcher: root.querySelector(".launcher"),
    panel: root.querySelector(".panel"),
    title: root.querySelector(".title"),
    dot: root.querySelector(".dot"),
    statusText: root.querySelector(".status-text"),
    log: root.querySelector(".log"),
    form: root.querySelector("form"),
    input: root.querySelector("input"),
    send: root.querySelector(".send"),
    mic: root.querySelector(".mic"),
    end: root.querySelector(".end"),
    again: root.querySelector(".again"),
    close: root.querySelector(".close"),
  };
  root.querySelector(".launcher-label").textContent = LABEL;
  el.title.textContent = "Voice assistant";

  // ---------- small helpers ----------

  function setState(name, customText) {
    var state = STATES[name] || STATES.connecting;
    el.dot.className = "dot " + state.tone;
    el.statusText.textContent = customText || state.text;
  }

  function scrollDown() {
    el.log.scrollTop = el.log.scrollHeight;
  }

  function addBubble(who, text) {
    var bubble = document.createElement("div");
    bubble.className = "msg " + who;
    bubble.textContent = text;
    el.log.appendChild(bubble);
    scrollDown();
    return bubble;
  }

  function addNote(text) {
    var note = document.createElement("div");
    note.className = "note";
    note.textContent = text;
    el.log.appendChild(note);
    scrollDown();
  }

  function setMicLabel() {
    el.mic.textContent = micOn ? "Mute microphone" : "Turn microphone on";
    el.mic.setAttribute("aria-pressed", micOn ? "false" : "true");
  }

  function setInCall(inCall) {
    el.end.hidden = !inCall;
    el.again.hidden = inCall;
    el.mic.disabled = !inCall;
    el.input.disabled = !inCall;
    el.send.disabled = !inCall;
  }

  function loadLivekit() {
    if (window.LivekitClient) return Promise.resolve(window.LivekitClient);
    if (!livekitLoading) {
      livekitLoading = new Promise(function (resolve, reject) {
        var tag = document.createElement("script");
        tag.src = LIVEKIT_SCRIPT;
        tag.onload = function () {
          resolve(window.LivekitClient);
        };
        tag.onerror = function () {
          livekitLoading = null;
          reject(new Error("Could not load the call library."));
        };
        document.head.appendChild(tag);
      });
    }
    return livekitLoading;
  }

  // ---------- the call ----------

  function showAgentState(participant) {
    var state = participant && participant.attributes && participant.attributes["lk.agent.state"];
    if (state) setState(state);
  }

  // Words arrive in pieces. Each piece of speech has a segment id: a new stream
  // with a known id replaces that bubble's text (this is how a live transcript
  // of the visitor's speech gets corrected as they talk).
  function onTranscript(reader, participantInfo) {
    var attributes = (reader.info && reader.info.attributes) || {};
    var isUser = room && participantInfo.identity === room.localParticipant.identity;
    var id = attributes["lk.segment_id"] || reader.info.id;
    var bubble = bubbles[id];
    var text = "";

    (async function () {
      try {
        for await (var chunk of reader) {
          text += chunk;
          if (!text.trim()) continue;
          if (!bubble) {
            bubble = addBubble(isUser ? "user" : "agent", "");
            bubbles[id] = bubble;
          }
          bubble.textContent = text;
          scrollDown();
        }
      } catch (error) {
        /* the call ended while text was arriving */
      }
    })();
  }

  async function startCall() {
    if (room) return;
    el.log.textContent = "";
    bubbles = {};
    setInCall(true);
    setState("connecting");

    try {
      var LK = await loadLivekit();
      var response = await fetch(SERVER + "/api/call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clinic_id: CLINIC }),
      });
      if (!response.ok) {
        var problem = await response.json().catch(function () {
          return {};
        });
        throw new Error(problem.detail || "The call could not be started.");
      }
      var ticket = await response.json();

      room = new LK.Room();
      room.on(LK.RoomEvent.TrackSubscribed, function (track) {
        if (track.kind !== "audio") return;
        var audio = track.attach();
        audio.style.display = "none";
        root.appendChild(audio);
        audioElements.push(audio);
      });
      room.on(LK.RoomEvent.ParticipantAttributesChanged, function (changed, participant) {
        if (changed && "lk.agent.state" in changed) showAgentState(participant);
      });
      room.on(LK.RoomEvent.ParticipantConnected, showAgentState);
      room.on(LK.RoomEvent.Disconnected, function () {
        endCall(true);
      });
      room.registerTextStreamHandler("lk.transcription", onTranscript);

      await room.connect(ticket.url, ticket.token);
      room.remoteParticipants.forEach(showAgentState);

      try {
        await room.localParticipant.setMicrophoneEnabled(true);
        micOn = true;
      } catch (micError) {
        micOn = false;
        addNote("Microphone not available. You can type your messages instead.");
      }
      setMicLabel();
    } catch (error) {
      console.error("Clinic widget:", error);
      endCall(false);
      setState("error");
      addNote(error && error.message ? error.message : "The call could not be started.");
    }
  }

  function endCall(fromRemote) {
    var current = room;
    room = null;
    micOn = false;
    audioElements.forEach(function (audio) {
      audio.remove();
    });
    audioElements = [];
    if (current && !fromRemote) current.disconnect();
    setInCall(false);
    setMicLabel();
    setState("ended");
  }

  // ---------- buttons ----------

  el.launcher.addEventListener("click", function () {
    el.panel.classList.add("open");
    el.launcher.style.display = "none";
    startCall();
  });

  el.close.addEventListener("click", function () {
    if (room) endCall(false);
    el.panel.classList.remove("open");
    el.launcher.style.display = "";
    el.launcher.focus();
  });

  el.end.addEventListener("click", function () {
    endCall(false);
  });

  el.again.addEventListener("click", startCall);

  el.mic.addEventListener("click", async function () {
    if (!room) return;
    try {
      await room.localParticipant.setMicrophoneEnabled(!micOn);
      micOn = !micOn;
    } catch (error) {
      addNote("Microphone not available. Check your browser's permission for this site.");
    }
    setMicLabel();
  });

  el.form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var text = el.input.value.trim();
    if (!text || !room) return;
    el.input.value = "";
    addBubble("user", text);
    try {
      await room.localParticipant.sendText(text, { topic: "lk.chat" });
    } catch (error) {
      addNote("That message could not be sent.");
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && el.panel.classList.contains("open")) el.close.click();
  });

  // ---------- start ----------

  setInCall(false);
  setMicLabel();
  setState("ended", "Ready");

  function mount() {
    document.body.appendChild(host);
    fetch(SERVER + "/api/clinics/" + encodeURIComponent(CLINIC))
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (clinic) {
        if (clinic && clinic.name) el.title.textContent = clinic.name;
      })
      .catch(function () {});
  }

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();