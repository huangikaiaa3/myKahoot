const app = document.querySelector('#app');
let socket;
let screen = 'home';
let state = { phase: 'lobby' };
let joined = false;
let selectedAnswer = null;
let answerResult = null;

function connect() {
  if (socket?.readyState === WebSocket.OPEN) return;
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onmessage = ({ data }) => receive(JSON.parse(data));
  socket.onclose = () => setTimeout(connect, 1200);
}

function emit(action, payload = {}) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ action, ...payload }));
}

function layout(inner, mode = '') {
  app.innerHTML = `<section class="shell ${mode}"><h1 class="brand">詣翔 <i>&amp;</i> 慈晏</h1>${inner}</section>`;
}

function optionTiles(answers, className, selected = null, disabled = false, correct = null) {
  const marks = ['▲', '◆', '●', '■'];
  const tag = className === 'choice' ? 'button' : 'div';
  return answers.map((answer, index) => `<${tag} class="${className} ${selected === index ? 'selected' : ''} ${correct === index ? 'is-correct' : ''}" ${className === 'choice' && disabled ? 'disabled' : ''} ${className === 'choice' ? `onclick="answer(${index})"` : ''}><span class="answer-shape">${marks[index]}</span><span>${answer}</span></${tag}>`).join('');
}

function home() {
  screen = 'home';
  layout(`<div class="card"><div class="eyebrow">即時問答</div><h2 class="title">準備好展現你的實力了嗎？</h2><p class="muted">用手機加入房間，快速作答，爭取高分。</p><div class="host-tools"><button class="primary" onclick="joinForm()">加入遊戲</button><button class="secondary" onclick="host()">主持人控制台</button></div></div>`);
}

function joinForm() {
  screen = 'join';
  layout(`<div class="card"><div class="eyebrow">玩家加入</div><h2 class="title">準備好了嗎？</h2><form class="form" onsubmit="join(event)"><label>遊戲代碼<input required inputmode="numeric" name="pin" maxlength="6" placeholder="000000"></label><label>你的名稱<input required name="name" maxlength="24" placeholder="王小明"></label><button class="primary">進入遊戲</button></form><div id="notice"></div></div>`);
}

function join(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  emit('join', { pin: form.get('pin'), name: form.get('name') });
}

function host() {
  screen = 'host';
  emit('host');
  layout(`<div class="card"><div class="eyebrow">主持人控制台</div><h2 class="title">正在開啟房間…</h2></div>`);
}

function hostView() {
  const qrUrl = encodeURIComponent(location.origin);
  if (state.phase === 'lobby') {
    layout(`<div class="card host-lobby"><div class="eyebrow">遊戲大廳</div><div class="host-lobby-grid"><div><h2 class="title">使用遊戲代碼加入</h2><div class="pin">${state.pin}</div><span class="player-count">已有 ${state.players} 名玩家就緒</span></div><img class="qr" src="/join-qr.svg?url=${qrUrl}" alt="加入遊戲的二維碼"></div><div class="host-tools"><button class="primary" onclick="emit('next')">開始遊戲</button><button class="secondary" onclick="emit('reset')">建立新房間</button></div></div>`);
    return;
  }
  const rankings = state.phase === 'reveal' && state.standings?.length ? `<aside class="host-sidebar"><div class="eyebrow">目前排行榜</div><div class="leaderboard-scroll">${board(state.standings)}</div></aside>` : '';
  const action = state.phase === 'question'
    ? `<button class="secondary" onclick="emit('reveal')">立刻公布答案</button>`
    : `<button class="primary" onclick="emit('next')">${state.question_index + 1 === state.total ? '顯示最終排行榜' : '下一題'}</button>`;
  layout(`<div class="card stage ${rankings ? 'stage-with-leaderboard' : ''}">${rankings}<main class="stage-main"><div class="stage-top"><span>第 ${state.index + 1} / ${state.total} 題 · ${state.players} 名玩家</span><b class="stage-timer">${state.remaining ?? state.time_limit}</b></div><p class="host-question">${state.prompt}</p><div class="host-choices">${optionTiles(state.answers, 'host-choice', null, false, state.phase === 'reveal' ? state.correct : null)}</div><div class="stage-actions">${action}<button class="secondary" onclick="emit('restart')">重新開始</button></div></main></div>`, 'play-shell');
}

function playerLobby() {
  layout(`<div class="card answer-state"><div><div class="eyebrow">你已加入</div><h2 class="title">準備作答。</h2><p class="muted">請觀看共享螢幕，答題按鈕將出現在這裡。</p></div></div>`, 'play-shell');
}

function questionView() {
  const feedback = answerResult ? `<div class="answer-feedback"><strong>答案已鎖定</strong><span>題目結束後將公布結果。</span></div>` : '';
  layout(`<div class="card player-card"><div class="stage-top"><span>第 ${state.index + 1} / ${state.total} 題</span><b class="stage-timer">${state.remaining ?? state.time_limit}</b></div><h2 class="player-question">${state.prompt}</h2><div class="choices">${optionTiles(state.answers, 'choice', selectedAnswer, selectedAnswer !== null)}</div>${feedback}</div>`, 'play-shell');
}

function answer(index) {
  if (selectedAnswer !== null) return;
  selectedAnswer = index;
  emit('answer', { answer: index });
  questionView();
}

function revealView() {
  const result = answerResult ? `<p class="muted">${answerResult.correct ? `本題獲得 ${answerResult.points} 分。` : '本題未獲得分數。'} 總分：${answerResult.total_score} · 目前排名：第 ${answerResult.rank} 名</p>` : '';
  layout(`<div class="card answer-state"><div><div class="eyebrow">正確答案</div><h2 class="title">${state.prompt}</h2><div class="correct-answer">${state.answers[state.correct]}</div>${result}</div></div>`, 'play-shell');
}

function board(rows) {
  return `<ol class="leaderboard">${rows.map(row => `<li><b>${row.rank}</b><span>${row.name}</span><b>${row.score}</b></li>`).join('')}</ol>`;
}

function podium() {
  const controls = screen === 'host'
    ? `<div class="stage-actions"><button class="primary" onclick="emit('restart')">重設遊戲</button><button class="secondary" onclick="emit('reset')">建立新房間</button></div>`
    : '';
  layout(`<div class="card final-card">${board(state.standings)}${controls}</div>`, 'play-shell');
}

function receive(message) {
  if (message.type === 'error') {
    document.querySelector('#notice').innerHTML = `<div class="notice">${message.message}</div>`;
    return;
  }
  if (message.type === 'joined') {
    joined = true;
    playerLobby();
    return;
  }
  if (message.type === 'restarted') {
    selectedAnswer = null;
    answerResult = null;
    state = { ...state, phase: 'lobby', question_index: -1, remaining: 0 };
    if (joined) playerLobby();
    return;
  }
  if (message.type === 'host_status') {
    state = { ...state, ...message, ...(message.current_question || {}) };
    if (state.phase === 'podium' && (joined || screen === 'host')) podium();
    else if (screen === 'host') hostView();
    return;
  }
  if (message.type === 'question') {
    selectedAnswer = null;
    answerResult = null;
    state = { ...state, ...message, phase: 'question' };
    if (joined) questionView();
    return;
  }
  if (message.type === 'answer_received') {
    answerResult = { locked: true };
    if (joined) questionView();
    return;
  }
  if (message.type === 'timer') {
    state = { ...state, ...message };
    if (joined && state.phase === 'question') questionView();
    if (screen === 'host' && state.phase === 'question') hostView();
    return;
  }
  if (message.type === 'reveal') {
    state = { ...state, ...message, phase: 'reveal' };
    if (joined) revealView();
    if (screen === 'host') hostView();
    return;
  }
  if (message.type === 'round_result') {
    answerResult = message;
    if (joined && state.phase === 'reveal') revealView();
    return;
  }
  if (message.type === 'podium') {
    state = { ...state, ...message, phase: 'podium' };
    if (joined || screen === 'host') podium();
  }
}

connect();
home();
