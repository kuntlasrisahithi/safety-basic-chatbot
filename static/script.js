const chat = document.getElementById('chat');
const form = document.getElementById('form');
const input = document.getElementById('query');

function addMessage(text, who, extra) {
  const div = document.createElement('div');
  div.className = 'msg ' + who;
  div.innerHTML = text + (extra || '');
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function ask(query) {
  addMessage(query, 'user');
  input.value = '';
  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query })
    });
    const data = await res.json();

    let extra = '<div class="confidence ' + data.confidence + '">' + data.confidence + ' confidence</div>';
    if (data.sources && data.sources.length) {
      extra += '<div class="sources">Matched: ' + data.sources.map(s => s.category).join(', ') + '</div>';
    }
    addMessage(data.answer, 'bot', extra);
  } catch (err) {
    addMessage('Something went wrong talking to the local server.', 'bot');
  }
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (q) ask(q);
});

function askChip(cat) {
  ask('What is ' + cat + '?');
}

addMessage("Hi! I'm a fully local safety assistant — ask me anything from the dataset.", 'bot');