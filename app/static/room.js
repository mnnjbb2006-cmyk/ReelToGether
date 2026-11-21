(async () => {
  const roomId = window.REEL_ROOM_ID;
  const query = new URLSearchParams(window.location.search);
  const myName = (query.get('name') || 'Guest').trim().slice(0, 24) || 'Guest';

  const $ = (id) => document.getElementById(id);
  const movie = $('movie');
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  let socket;
  const peers = new Map();
  const remoteAudios = new Map();
  const participantMap = new Map();

  let myPeerId = null;
  let hostId = null;
  let localStream = null;
  let micOn = false;
  let suppressPlaybackEvents = false;
  let latestPlayback = null;
  let lastSyncSentAt = 0;
  let toastTimer = null;

  let rtcConfig = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };
  try {
    const configResponse = await fetch('/api/rtc-config');
    if (configResponse.ok) rtcConfig = await configResponse.json();
  } catch (error) { console.warn('Using fallback ICE configuration:', error); }
  socket = new WebSocket(`${wsProtocol}//${window.location.host}/ws/${roomId}?name=${encodeURIComponent(myName)}`);

  function send(payload) {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
  }

  function showToast(message) {
    const toast = $('toast');
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
  }

  function setConnection(connected, label) {
    $('connection-label').textContent = label || (connected ? 'CONNECTED' : 'DISCONNECTED');
    $('connection-label').previousElementSibling.classList.toggle('connected', connected);
  }

  function isHost() {
    return Boolean(myPeerId && hostId === myPeerId);
  }

  function updateHostUI() {
    const host = isHost();
    movie.controls = host;
    $('host-indicator').textContent = host ? '★ YOU’RE THE HOST' : '↗ HOST CONTROLS';
    $('host-only-tag').textContent = host ? 'YOU ARE HOST' : 'HOST ONLY';
    $('video-url').disabled = !host;
    $('source-form').querySelector('button').disabled = !host;
    $('video-url').placeholder = host ? 'Paste a direct MP4 / WebM video URL…' : 'Only the host can change the movie';
    $('source-help').textContent = host
      ? 'Use a direct video file URL that your friends are allowed to access. YouTube, Netflix and other DRM-protected pages are not direct video URLs.'
      : 'The room host controls the movie and playback. You can still chat and talk with everyone.';
    if (!host) $('playback-hint').textContent = 'The host controls shared playback.';
  }

  function initials(name) {
    const parts = String(name || '?').trim().split(/\s+/).slice(0, 2);
    return parts.map(part => part[0] || '').join('').toUpperCase() || '?';
  }

  function renderPeople() {
    const list = $('people-list');
    const people = [...participantMap.values()];
    $('people-count').textContent = String(people.length);
    list.replaceChildren();
    if (!people.length) {
      const empty = document.createElement('div');
      empty.className = 'people-loading';
      empty.textContent = 'Waiting for the crew…';
      list.append(empty);
      return;
    }
    people.forEach(person => {
      const row = document.createElement('div');
      row.className = 'person-row';
      const avatar = document.createElement('div');
      avatar.className = 'person-avatar';
      avatar.textContent = initials(person.name);
      const name = document.createElement('div');
      name.className = 'person-name';
      name.textContent = person.name + (person.peer_id === myPeerId ? ' (you)' : '');
      const tags = document.createElement('div');
      tags.className = 'person-tags';
      if (person.peer_id === hostId) {
        const hostTag = document.createElement('span');
        hostTag.className = 'person-tag';
        hostTag.textContent = 'HOST';
        tags.append(hostTag);
      }
      const mic = document.createElement('span');
      mic.className = `person-mic${person.mic_on ? ' on' : ''}`;
      mic.textContent = person.mic_on ? '♫' : '♪̸';
      mic.title = person.mic_on ? 'Microphone on' : 'Microphone off';
      tags.append(mic);
      row.append(avatar, name, tags);
      list.append(row);
    });
  }

  function upsertPerson(person) {
    if (!person || !person.peer_id) return;
    participantMap.set(person.peer_id, person);
    renderPeople();
  }

  function removePerson(peerId) {
    participantMap.delete(peerId);
    const pc = peers.get(peerId);
    if (pc) {
      pc.close();
      peers.delete(peerId);
    }
    const audio = remoteAudios.get(peerId);
    if (audio) {
      audio.pause();
      audio.srcObject = null;
      audio.remove();
      remoteAudios.delete(peerId);
    }
    renderPeople();
  }

  function addChatMessage(message) {
    const wrap = $('chat-messages');
    const empty = wrap.querySelector('.chat-empty');
    if (empty) empty.remove();

    const item = document.createElement('div');
    item.className = `chat-message${message.peer_id === myPeerId ? ' mine' : ''}`;
    const meta = document.createElement('div');
    meta.className = 'chat-meta';
    const sender = document.createElement('strong');
    sender.textContent = message.name || 'Guest';
    const time = document.createElement('time');
    time.textContent = new Date((message.sent_at || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    meta.append(sender, time);
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.textContent = message.text || '';
    item.append(meta, bubble);
    wrap.append(item);
    wrap.scrollTop = wrap.scrollHeight;
  }

  function displayReaction(emoji, name) {
    const holder = $('reaction-float');
    const pop = document.createElement('div');
    pop.className = 'reaction-pop';
    pop.textContent = `${emoji}  ${name || ''}`;
    holder.append(pop);
    while (holder.children.length > 4) holder.firstElementChild.remove();
    setTimeout(() => pop.remove(), 2500);
  }

  function getRemoteAudio(peerId) {
    let audio = remoteAudios.get(peerId);
    if (!audio) {
      audio = document.createElement('audio');
      audio.autoplay = true;
      audio.playsInline = true;
      audio.dataset.peerId = peerId;
      audio.style.display = 'none';
      document.body.append(audio);
      remoteAudios.set(peerId, audio);
    }
    return audio;
  }

  function makePeer(peerId) {
    if (peers.has(peerId)) return peers.get(peerId);
    const pc = new RTCPeerConnection(rtcConfig);
    const state = {
      pc,
      polite: Boolean(myPeerId && myPeerId.localeCompare(peerId) > 0),
      makingOffer: false,
      ignoreOffer: false,
      isSettingRemoteAnswerPending: false
    };
    peers.set(peerId, pc);
    pc.__reelState = state;

    if (localStream && micOn) {
      localStream.getAudioTracks().forEach(track => pc.addTrack(track, localStream));
    }

    pc.onicecandidate = (event) => {
      if (event.candidate) send({ type: 'signal', to: peerId, signal: { candidate: event.candidate.toJSON() } });
    };

    pc.ontrack = (event) => {
      const audio = getRemoteAudio(peerId);
      if (event.streams && event.streams[0]) {
        audio.srcObject = event.streams[0];
      } else {
        const stream = audio.srcObject || new MediaStream();
        stream.addTrack(event.track);
        audio.srcObject = stream;
      }
      audio.play().catch(() => {
        showToast('Click anywhere in the room to enable incoming voice audio.');
      });
    };

    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed') {
        showToast('Voice connection failed. A TURN server may be needed on this network.');
      }
    };

    pc.onnegotiationneeded = async () => {
      try {
        state.makingOffer = true;
        await pc.setLocalDescription();
        send({ type: 'signal', to: peerId, signal: { description: pc.localDescription } });
      } catch (error) {
        console.warn('WebRTC negotiation error:', error);
      } finally {
        state.makingOffer = false;
      }
    };
    return pc;
  }

  async function handleSignal(fromPeer, signal) {
    const pc = makePeer(fromPeer);
    const state = pc.__reelState;
    try {
      if (signal.description) {
        const description = signal.description;
        const offerCollision = description.type === 'offer' &&
          (state.makingOffer || (pc.signalingState !== 'stable' && !state.isSettingRemoteAnswerPending));
        state.ignoreOffer = !state.polite && offerCollision;
        if (state.ignoreOffer) return;

        state.isSettingRemoteAnswerPending = description.type === 'answer';
        if (offerCollision) {
          await Promise.all([
            pc.setLocalDescription({ type: 'rollback' }),
            pc.setRemoteDescription(description)
          ]);
        } else {
          await pc.setRemoteDescription(description);
        }
        state.isSettingRemoteAnswerPending = false;

        if (description.type === 'offer') {
          await pc.setLocalDescription();
          send({ type: 'signal', to: fromPeer, signal: { description: pc.localDescription } });
        }
      } else if (signal.candidate) {
        try {
          await pc.addIceCandidate(signal.candidate);
        } catch (error) {
          if (!state.ignoreOffer) throw error;
        }
      }
    } catch (error) {
      console.warn('Could not process WebRTC signal:', error);
    }
  }

  function applyPlayback(playback) {
    if (!playback) return;
    latestPlayback = playback;
    const source = playback.source || '';
    if (!source) {
      $('video-empty').classList.remove('hidden');
      $('now-playing').textContent = 'Waiting for a movie';
      $('playback-hint').textContent = 'Your room is ready when you are.';
      $('tap-to-play').classList.add('hidden');
      return;
    }

    $('video-empty').classList.add('hidden');
    const sourceChanged = movie.src !== new URL(source, window.location.href).href;
    if (sourceChanged) {
      movie.src = source;
      movie.load();
      $('now-playing').textContent = friendlyVideoName(source);
    }
    $('playback-hint').textContent = playback.playing ? 'Playing in sync with the room.' : 'Paused — waiting for the next scene.';

    const syncToState = () => {
      if (!latestPlayback || !latestPlayback.source) return;
      const elapsed = latestPlayback.playing ? Math.max(0, Date.now() / 1000 - (latestPlayback.updated_at || Date.now() / 1000)) : 0;
      const targetTime = Math.max(0, Number(latestPlayback.current_time || 0) + elapsed);
      suppressPlaybackEvents = true;
      if (Number.isFinite(targetTime) && Math.abs(movie.currentTime - targetTime) > 1.25) {
        try { movie.currentTime = targetTime; } catch (_) { /* metadata may not be ready */ }
      }
      if (latestPlayback.playing) {
        movie.play().then(() => $('tap-to-play').classList.add('hidden')).catch(() => $('tap-to-play').classList.remove('hidden'));
      } else {
        movie.pause();
        $('tap-to-play').classList.add('hidden');
      }
      setTimeout(() => { suppressPlaybackEvents = false; }, 900);
    };

    if (movie.readyState >= 1) syncToState();
    else movie.addEventListener('loadedmetadata', syncToState, { once: true });
  }

  function friendlyVideoName(url) {
    try {
      const parsed = new URL(url);
      const last = decodeURIComponent(parsed.pathname.split('/').filter(Boolean).pop() || 'Shared video');
      return last.replace(/\.(mp4|webm|ogg|mov|m4v)$/i, '').replace(/[-_]/g, ' ').slice(0, 50) || 'Shared video';
    } catch (_) { return 'Shared video'; }
  }

  function sendPlayback(action) {
    if (!isHost() || suppressPlaybackEvents || !movie.src) return;
    send({ type: 'playback', action, current_time: Number.isFinite(movie.currentTime) ? movie.currentTime : 0 });
  }

  async function toggleMic() {
    const button = $('mic-toggle');
    button.disabled = true;
    if (!localStream) {
      try {
        localStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          video: false
        });
      } catch (error) {
        console.error('Microphone permission error:', error);
        button.disabled = false;
        button.classList.add('denied');
        $('voice-status').textContent = 'Microphone access was blocked';
        showToast('Please allow microphone access in your browser settings.');
        return;
      }
    }

    micOn = !micOn;
    localStream.getAudioTracks().forEach(track => { track.enabled = micOn; });
    if (micOn) {
      peers.forEach(pc => {
        const alreadyAdded = pc.getSenders().some(sender => sender.track && sender.track.kind === 'audio');
        if (!alreadyAdded) localStream.getAudioTracks().forEach(track => pc.addTrack(track, localStream));
      });
    }
    button.disabled = false;
    button.classList.toggle('active', micOn);
    button.classList.remove('denied');
    button.setAttribute('aria-pressed', String(micOn));
    $('mic-label').textContent = micOn ? 'Turn mic off' : 'Turn mic on';
    $('voice-status').textContent = micOn ? 'Your mic is live — say hello!' : 'Your mic is off';
    send({ type: 'mic', enabled: micOn });
    const me = participantMap.get(myPeerId);
    if (me) { me.mic_on = micOn; renderPeople(); }
  }

  function handleServerMessage(message) {
    switch (message.type) {
      case 'welcome':
        myPeerId = message.peer_id;
        hostId = message.host_id;
        participantMap.clear();
        (message.participants || []).forEach(person => {
          upsertPerson(person);
          if (person.peer_id !== myPeerId) makePeer(person.peer_id);
        });
        setConnection(true, 'CONNECTED');
        updateHostUI();
        applyPlayback(message.playback);
        break;
      case 'participant_joined':
        upsertPerson(message.participant);
        if (message.participant && message.participant.peer_id !== myPeerId) makePeer(message.participant.peer_id);
        showToast(`${message.participant.name} joined the room`);
        break;
      case 'participant_left':
        removePerson(message.peer_id);
        if (message.host_changed) {
          hostId = message.host_id;
          updateHostUI();
          showToast(isHost() ? 'You are now the room host.' : 'The room host changed.');
        }
        renderPeople();
        break;
      case 'participant_updated': {
        const person = participantMap.get(message.peer_id);
        if (person) { person.mic_on = Boolean(message.mic_on); renderPeople(); }
        break;
      }
      case 'chat':
        addChatMessage(message.message);
        break;
      case 'signal':
        handleSignal(message.from, message.signal || {});
        break;
      case 'playback_state':
        applyPlayback(message.playback);
        break;
      case 'reaction':
        displayReaction(message.emoji, message.name);
        break;
      case 'error':
        showToast(message.message || 'Something went wrong.');
        break;
      default:
        break;
    }
  }

  socket.addEventListener('open', () => setConnection(false, 'JOINING'));
  socket.addEventListener('message', event => {
    try { handleServerMessage(JSON.parse(event.data)); }
    catch (error) { console.warn('Invalid room message:', error); }
  });
  socket.addEventListener('close', () => {
    setConnection(false, 'RECONNECT NEEDED');
    showToast('You left the room connection. Refresh to rejoin.');
  });
  socket.addEventListener('error', () => setConnection(false, 'CONNECTION ERROR'));

  $('copy-invite').addEventListener('click', async () => {
    const link = window.location.href;
    try {
      await navigator.clipboard.writeText(link);
      showToast('Invite link copied — send it to your friends!');
    } catch (_) {
      window.prompt('Copy this invite link:', link);
    }
  });

  $('source-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!isHost()) return showToast('Only the room host can change the movie.');
    const url = $('video-url').value.trim();
    if (!url) return showToast('Paste a direct video URL first.');
    try {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('protocol');
    } catch (_) {
      return showToast('Enter a valid http or https video URL.');
    }
    send({ type: 'source', url });
    $('video-url').value = '';
  });

  $('mic-toggle').addEventListener('click', toggleMic);
  $('chat-form').addEventListener('submit', event => {
    event.preventDefault();
    const input = $('chat-input');
    const text = input.value.trim();
    if (!text) return;
    send({ type: 'chat', text });
    input.value = '';
    input.focus();
  });
  document.querySelectorAll('[data-emoji]').forEach(button => {
    button.addEventListener('click', () => send({ type: 'reaction', emoji: button.dataset.emoji }));
  });

  movie.addEventListener('play', () => sendPlayback('play'));
  movie.addEventListener('pause', () => sendPlayback('pause'));
  movie.addEventListener('seeked', () => sendPlayback('seek'));
  movie.addEventListener('timeupdate', () => {
    if (!isHost() || suppressPlaybackEvents || !movie.src) return;
    const now = Date.now();
    if (now - lastSyncSentAt > 10000) {
      lastSyncSentAt = now;
      sendPlayback('sync');
    }
  });
  movie.addEventListener('error', () => {
    if (movie.currentSrc) showToast('Could not play this URL. Check that it is a direct, accessible video file.');
  });
  $('tap-to-play').addEventListener('click', () => {
    if (!movie.src) return;
    if (latestPlayback && latestPlayback.playing) {
      const elapsed = Math.max(0, Date.now() / 1000 - (latestPlayback.updated_at || Date.now() / 1000));
      try { movie.currentTime = Math.max(0, latestPlayback.current_time + elapsed); } catch (_) { }
    }
    movie.play().then(() => $('tap-to-play').classList.add('hidden')).catch(() => showToast('Playback is not available yet.'));
  });

  document.addEventListener('click', () => {
    remoteAudios.forEach(audio => audio.play().catch(() => {}));
  });

  document.addEventListener('keydown', event => {
    const target = event.target;
    const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
    if (typing || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key.toLowerCase() === 'm') {
      event.preventDefault();
      $('mic-toggle').click();
    }
    if (event.code === 'Space' && isHost() && movie.src) {
      event.preventDefault();
      if (movie.paused) movie.play().catch(() => {}); else movie.pause();
    }
  });

  window.addEventListener('beforeunload', () => {
    if (socket.readyState === WebSocket.OPEN) socket.close();
    peers.forEach(pc => pc.close());
    if (localStream) localStream.getTracks().forEach(track => track.stop());
  });
})();
