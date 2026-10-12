import { DialtClient } from '/sdk/index.js';
import { agents, initialMode, transferLanguage } from './agents.js';

const $ = (id) => document.getElementById(id);
let client = null;
let language = 'en';
let busy = false;
let lastAssistantLine = null;

function status(text) { $('status').textContent = text; }
function showAgent(nextLanguage) {
  language = nextLanguage;
  const agent = agents[language];
  $('voice-name').textContent = agent.voiceLabel;
  $('agent-language').textContent = agent.language;
  for (const node of document.querySelectorAll('[data-agent]')) {
    node.setAttribute('aria-current', String(node.dataset.agent === language));
  }
}
function append(speaker, text, locale = 'en') {
  if (!text) return;
  const line = document.createElement('p');
  const label = document.createElement('strong');
  label.textContent = `${speaker}: `;
  const content = document.createElement('span');
  content.textContent = text;
  content.lang = locale;
  line.append(label, content);
  $('transcript').hidden = false;
  $('transcript').append(line);
  line.scrollIntoView({ block: 'nearest' });
  return line;
}
function stop(message = 'Conversation ended') {
  const previous = client;
  client = null;
  previous?.close();
  busy = false;
  $('call-button').disabled = false;
  $('button-label').textContent = 'Start conversation';
  status(message);
}
function fail(message) {
  $('error').textContent = message;
  $('error').hidden = false;
  stop('Could not continue');
}

$('call-button').addEventListener('click', async () => {
  if (client) { stop(); return; }
  if (busy) return;
  busy = true;
  $('call-button').disabled = true;
  $('error').hidden = true;
  $('transcript').replaceChildren();
  $('transcript').hidden = true;
  lastAssistantLine = null;
  showAgent('en');
  status('Connecting…');
  // Unlock playback directly in the user gesture, before the credential fetch.
  const session = new DialtClient({ mode: initialMode });
  client = session;
  try {
    await session.unlockAudio();
    const response = await fetch('/session', { method: 'POST' });
    const credential = await response.json();
    if (!response.ok) throw new Error(credential.error || 'Could not start the conversation.');
    // Set the bound credential pair before opening the connection.
    session.sessionId = credential.session_id;
    session.apiKey = credential.api_key;
    session.addEventListener('asr', ({ detail }) => append('You', detail.text));
    session.addEventListener('utterance', ({ detail }) => {
      if (detail.corrected && lastAssistantLine) {
        lastAssistantLine.querySelector('span').textContent = detail.text;
      } else {
        lastAssistantLine = append('Assistant', detail.text, agents[language].locale);
      }
    });
    session.addEventListener('tool_call', async ({ detail }) => {
      if (client !== session) return;
      const previousLanguage = language;
      status('Transferring…');
      try {
        const next = await transferLanguage(session, detail, language);
        if (client !== session) return;
        if (next !== previousLanguage) append('Handoff', `${agents[previousLanguage].voiceLabel} → ${agents[next].voiceLabel}`);
        showAgent(next);
        status('Listening');
      } catch (error) {
        if (client === session) {
          $('error').textContent = `Transfer failed: ${error.message}`;
          $('error').hidden = false;
          status('Still connected');
        }
      }
    });
    session.addEventListener('voice', ({ detail }) => {
      const match = Object.entries(agents).find(([, agent]) => agent.voice === detail.voice);
      if (match) showAgent(match[0]);
    });
    for (const [event, text] of Object.entries({ warming_up: 'Opening microphone…',
      recovering: 'Recovering microphone…', listening: 'Listening', reconnecting: 'Reconnecting…',
      reconnected: 'Listening', turn: 'Speaking…', done: 'Listening' })) {
      session.addEventListener(event, () => { if (client === session) status(text); });
    }
    session.addEventListener('error', ({ detail }) => {
      if (client === session) fail(detail?.detail || detail?.error?.message || 'Connection failed. Please try again.');
    });
    session.addEventListener('failed', () => { if (client === session) fail('Microphone unavailable. Check its permission and try again.'); });
    session.addEventListener('session_end', () => { if (client === session) stop(); });
    await session.connect();
    if (client !== session) return;
    await session.startMic();
    if (client !== session) return;
    busy = false;
    $('call-button').disabled = false;
    $('button-label').textContent = 'End conversation';
  } catch (error) {
    if (client === session) fail(error.message);
  }
});

window.addEventListener('pagehide', () => client?.close());
