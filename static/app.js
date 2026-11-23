let inputMode = 'single'; // 'single' | 'batch'
let mediaType = 'video';  // 'video' | 'audio'
let rightTab = 'queue';   // 'queue' | 'history'

let lastPreviewInfo = null;
let lastPreviewUrl = '';
let availableVideoHeights = [];
let preferredAudioQuality = '320';
let analyzeAbortController = null;

let lastQueueSig = '';
let lastHistorySig = '';

const VIDEO_CONTAINERS = [
  { value: 'mp4', label: 'MP4 (標準・最も互換性が高い)' },
  { value: 'webm', label: 'WEBM (高画質・軽量)' },
  { value: 'mkv', label: 'MKV (全コーデック・字幕対応)' },
];

const AUDIO_CONTAINERS = [
  { value: 'mp3', label: 'MP3 (音楽・汎用オーディオ)' },
  { value: 'm4a', label: 'M4A / AAC (高音質・Apple等)' },
  { value: 'wav', label: 'WAV (非圧縮・無劣化)' },
  { value: 'flac', label: 'FLAC (可逆圧縮・ハイレゾ相当)' },
  { value: 'opus', label: 'OPUS (高効率・高音質)' },
];

const DEFAULT_VIDEO_QUALITIES = [
  { value: 'best', label: '最高画質 (自動選択)' },
  { value: '2160', label: '4K (2160p)' },
  { value: '1440', label: '2K / WQHD (1440p)' },
  { value: '1080', label: 'フルHD (1080p)' },
  { value: '720', label: 'HD (720p)' },
  { value: '480', label: '標準 (480p)' },
  { value: '360', label: '軽量 (360p)' },
];

const AUDIO_QUALITIES = [
  { value: '320', label: '最高音質 (320 kbps)' },
  { value: '256', label: '高音質 (256 kbps)' },
  { value: '192', label: '標準高音質 (192 kbps)' },
  { value: '128', label: '軽量サイズ (128 kbps)' },
];

const FALLBACK_THUMB = 'data:image/svg+xml;utf8,' + encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90" viewBox="0 0 160 90">
  <rect width="160" height="90" fill="#111827"/>
  <circle cx="80" cy="45" r="18" fill="#1f2937"/>
  <polygon points="75,36 90,45 75,54" fill="#6366f1"/>
</svg>
`);

document.addEventListener('DOMContentLoaded', async () => {
  renderFormatSelectors();
  await loadInitialConfig();
  setupEventListeners();
  pollTasks();
  setInterval(pollTasks, 1000);
});

async function loadInitialConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    const saveDirInput = document.getElementById('saveDirInput');
    saveDirInput.value = data.default_save_dir || '';

    const badge = document.getElementById('ffmpegStatusBadge');
    if (data.ffmpeg_available) {
      badge.textContent = 'FFmpeg エンジン有効';
      badge.className = 'badge badge-ok';
    } else {
      badge.textContent = 'FFmpeg 未検出';
    }
  } catch (e) {
    console.error(e);
  }
}

async function persistSaveDir(dirPath) {
  const trimmed = (dirPath || '').trim();
  if (!trimmed) return;
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ save_dir: trimmed })
    });
  } catch (e) {
    // ignore transient save errors
  }
}

function setupEventListeners() {
  const urlInput = document.getElementById('urlInput');
  let debounceTimer = null;

  urlInput.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    const val = urlInput.value.trim();
    if (val !== lastPreviewUrl) {
      lastPreviewInfo = null;
      lastPreviewUrl = '';
    }
    if (val.startsWith('http://') || val.startsWith('https://')) {
      debounceTimer = setTimeout(() => {
        analyzeUrl(true);
      }, 650);
    } else if (!val) {
      hidePreview();
    }
  });

  urlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      analyzeUrl(false);
    }
  });

  const batchUrlsInput = document.getElementById('batchUrlsInput');
  batchUrlsInput.addEventListener('input', updateBatchCount);

  const saveDirInput = document.getElementById('saveDirInput');
  saveDirInput.addEventListener('change', () => {
    persistSaveDir(saveDirInput.value);
  });

  const containerSelect = document.getElementById('containerSelect');
  containerSelect.addEventListener('change', () => {
    if (mediaType === 'audio') {
      updateAudioQualityState();
    }
    updateEmbedThumbState();
  });

  const qualitySelect = document.getElementById('qualitySelect');
  qualitySelect.addEventListener('change', () => {
    if (mediaType === 'audio' && !qualitySelect.disabled && qualitySelect.value) {
      preferredAudioQuality = qualitySelect.value;
    }
  });

  // Event delegation for task lists (safe against quotes/special chars in paths & no inline onclick)
  const handleTaskListClick = (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'open-folder') {
      const targetPath = btn.dataset.path;
      if (targetPath) openFolderPath(targetPath);
    } else if (action === 'cancel') {
      const taskId = btn.dataset.taskId;
      if (taskId) cancelTask(taskId);
    }
  };

  document.getElementById('queueListContainer').addEventListener('click', handleTaskListClick);
  document.getElementById('historyListContainer').addEventListener('click', handleTaskListClick);
}

function updateBatchCount() {
  const raw = document.getElementById('batchUrlsInput').value;
  const lines = raw.split('\n').map(s => s.trim()).filter(s => s.startsWith('http'));
  document.getElementById('batchCountLabel').textContent = `${lines.length} 件の有効なURL`;
}

function switchInputMode(mode) {
  inputMode = mode;
  document.getElementById('singleModeBtn').classList.toggle('active', mode === 'single');
  document.getElementById('batchModeBtn').classList.toggle('active', mode === 'batch');
  document.getElementById('singleInputBox').classList.toggle('hidden', mode !== 'single');
  document.getElementById('batchInputBox').classList.toggle('hidden', mode !== 'batch');

  const btnText = document.getElementById('downloadBtnText');
  btnText.textContent = mode === 'batch' ? '一括ダウンロード開始' : 'ダウンロード開始';
}

function setMediaType(type) {
  mediaType = type;
  document.getElementById('tabVideo').classList.toggle('active', type === 'video');
  document.getElementById('tabAudio').classList.toggle('active', type === 'audio');
  document.getElementById('embedSubsLabel').classList.toggle('hidden', type === 'audio');
  renderFormatSelectors();
}

function updateEmbedThumbState() {
  const container = document.getElementById('containerSelect').value;
  const thumbCheck = document.getElementById('embedThumbCheck');
  const isWav = mediaType === 'audio' && container === 'wav';
  thumbCheck.disabled = isWav;
  if (isWav) {
    thumbCheck.checked = false;
  }
}

function updateAudioQualityState() {
  const container = document.getElementById('containerSelect').value;
  const qualitySelect = document.getElementById('qualitySelect');
  const isLossless = container === 'wav' || container === 'flac';

  qualitySelect.innerHTML = '';
  if (isLossless) {
    const opt = document.createElement('option');
    opt.value = 'lossless';
    opt.textContent = 'ロスレス（無劣化・ビットレート指定不要）';
    qualitySelect.appendChild(opt);
    qualitySelect.disabled = true;
  } else {
    qualitySelect.disabled = false;
    AUDIO_QUALITIES.forEach(q => {
      const opt = document.createElement('option');
      opt.value = q.value;
      opt.textContent = q.label;
      qualitySelect.appendChild(opt);
    });
    qualitySelect.value = AUDIO_QUALITIES.some(q => q.value === preferredAudioQuality)
      ? preferredAudioQuality
      : '320';
  }
  updateEmbedThumbState();
}

function renderFormatSelectors() {
  const containerSelect = document.getElementById('containerSelect');
  const qualitySelect = document.getElementById('qualitySelect');
  const qualityLabel = document.getElementById('qualityLabel');

  const prevContainer = containerSelect.value;
  const prevQuality = qualitySelect.value;

  containerSelect.innerHTML = '';
  qualitySelect.innerHTML = '';
  qualitySelect.disabled = false;

  if (mediaType === 'video') {
    VIDEO_CONTAINERS.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.value;
      opt.textContent = c.label;
      containerSelect.appendChild(opt);
    });
    if (VIDEO_CONTAINERS.some(c => c.value === prevContainer)) {
      containerSelect.value = prevContainer;
    }

    qualityLabel.textContent = '画質 (最大解像度)';
    if (availableVideoHeights.length > 0) {
      const bestOpt = document.createElement('option');
      bestOpt.value = 'best';
      bestOpt.textContent = `最高画質 (${availableVideoHeights[0]}p / 自動)`;
      qualitySelect.appendChild(bestOpt);

      availableVideoHeights.forEach(h => {
        const opt = document.createElement('option');
        opt.value = String(h);
        const tag = h >= 2160 ? '4K' : h >= 1440 ? '2K' : h >= 1080 ? 'Full HD' : h >= 720 ? 'HD' : 'SD';
        opt.textContent = `${h}p (${tag})`;
        qualitySelect.appendChild(opt);
      });
    } else {
      DEFAULT_VIDEO_QUALITIES.forEach(q => {
        const opt = document.createElement('option');
        opt.value = q.value;
        opt.textContent = q.label;
        qualitySelect.appendChild(opt);
      });
    }

    if ([...qualitySelect.options].some(o => o.value === prevQuality)) {
      qualitySelect.value = prevQuality;
    }
    updateEmbedThumbState();
  } else {
    AUDIO_CONTAINERS.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.value;
      opt.textContent = c.label;
      containerSelect.appendChild(opt);
    });
    if (AUDIO_CONTAINERS.some(c => c.value === prevContainer)) {
      containerSelect.value = prevContainer;
    }

    qualityLabel.textContent = '音質 (ビットレート)';
    updateAudioQualityState();
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const urlInput = document.getElementById('urlInput');
      urlInput.value = text.trim();
      analyzeUrl(false);
    }
  } catch (e) {
    showToast('クリップボードの読み取りが許可されていません。Ctrl+V で貼り付けてください。');
  }
}

async function pasteBatchClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const area = document.getElementById('batchUrlsInput');
      area.value = (area.value ? area.value.trim() + '\n' : '') + text.trim();
      updateBatchCount();
    }
  } catch (e) {
    showToast('Ctrl+V で貼り付けてください');
  }
}

function hidePreview() {
  if (analyzeAbortController) {
    analyzeAbortController.abort();
    analyzeAbortController = null;
  }
  lastPreviewInfo = null;
  lastPreviewUrl = '';
  availableVideoHeights = [];
  document.getElementById('previewCard').classList.add('hidden');
  document.getElementById('analyzeLoading').classList.add('hidden');
  renderFormatSelectors();
}

async function analyzeUrl(silent = false) {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) {
    if (!silent) showToast('URLを入力してください');
    return;
  }

  // Cancel any in-flight info request so stale responses never overwrite newer ones
  if (analyzeAbortController) {
    analyzeAbortController.abort();
  }
  const controller = new AbortController();
  analyzeAbortController = controller;

  const loadingEl = document.getElementById('analyzeLoading');
  const previewCard = document.getElementById('previewCard');
  loadingEl.classList.remove('hidden');

  try {
    const res = await fetch('/api/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
      signal: controller.signal,
    });
    const data = await res.json();
    if (controller.signal.aborted) return;

    if (!res.ok) {
      throw new Error(data.detail || '情報取得エラー');
    }

    lastPreviewInfo = data;
    lastPreviewUrl = url;
    availableVideoHeights = data.available_heights || [];
    renderFormatSelectors();

    const thumbEl = document.getElementById('previewThumb');
    thumbEl.src = data.thumbnail || FALLBACK_THUMB;
    thumbEl.onerror = () => { thumbEl.src = FALLBACK_THUMB; };

    document.getElementById('previewDuration').textContent = data.duration_str || '--:--';
    document.getElementById('previewSite').textContent = data.extractor || 'Media';
    document.getElementById('previewTitle').textContent = data.title || '無題のメディア';
    document.getElementById('previewUploader').textContent = `投稿者 / チャンネル: ${data.uploader || '-'}`;

    const resBadges = document.getElementById('previewResBadges');
    resBadges.innerHTML = '';
    if (availableVideoHeights.length > 0) {
      availableVideoHeights.slice(0, 6).forEach(h => {
        const span = document.createElement('span');
        span.className = 'res-tag';
        span.textContent = `${h}p対応`;
        resBadges.appendChild(span);
      });
    } else {
      const span = document.createElement('span');
      span.className = 'res-tag';
      span.textContent = '音声 / ストリーム対応';
      resBadges.appendChild(span);
    }

    previewCard.classList.remove('hidden');
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    if (!silent) {
      showToast(err.message);
    }
  } finally {
    if (analyzeAbortController === controller) {
      loadingEl.classList.add('hidden');
      analyzeAbortController = null;
    }
  }
}

async function pickSaveFolder() {
  const saveDirInput = document.getElementById('saveDirInput');
  try {
    const res = await fetch('/api/pick-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_dir: saveDirInput.value.trim() })
    });
    const data = await res.json();
    if (res.ok && data.selected && data.path) {
      saveDirInput.value = data.path;
      showToast(`保存先を変更しました: ${data.path}`);
    }
  } catch (e) {
    showToast('フォルダ選択ダイアログを開けませんでした');
  }
}

async function openCurrentSaveFolder() {
  const path = document.getElementById('saveDirInput').value.trim();
  if (!path) return;
  await openFolderPath(path);
}

async function openFolderPath(path) {
  try {
    const res = await fetch('/api/open-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    });
    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'フォルダを開けませんでした');
    }
  } catch (e) {
    showToast('フォルダを開けませんでした');
  }
}

async function startDownload() {
  const saveDir = document.getElementById('saveDirInput').value.trim();
  const container = document.getElementById('containerSelect').value;
  const quality = document.getElementById('qualitySelect').value;
  const startTime = document.getElementById('startTimeInput').value.trim();
  const endTime = document.getElementById('endTimeInput').value.trim();
  const customFilename = document.getElementById('customFilenameInput').value.trim();
  const embedThumb = document.getElementById('embedThumbCheck').checked;
  const embedSubs = document.getElementById('embedSubsCheck').checked;

  let urls = [];
  if (inputMode === 'single') {
    const u = document.getElementById('urlInput').value.trim();
    if (!u) {
      showToast('ダウンロードするURLを入力してください');
      return;
    }
    const hasMatchingPreview = lastPreviewInfo && lastPreviewUrl === u;
    urls.push({
      url: u,
      title_hint: hasMatchingPreview ? lastPreviewInfo.title : u,
      thumbnail_hint: hasMatchingPreview ? lastPreviewInfo.thumbnail : '',
    });
  } else {
    const raw = document.getElementById('batchUrlsInput').value;
    const lines = raw.split('\n').map(s => s.trim()).filter(s => s.startsWith('http'));
    if (lines.length === 0) {
      showToast('1つ以上の有効なURL（http〜）を入力してください');
      return;
    }
    urls = lines.map(u => ({ url: u, title_hint: u, thumbnail_hint: '' }));
  }

  const items = urls.map((u, idx) => ({
    url: u.url,
    save_dir: saveDir,
    media_type: mediaType,
    container: container,
    quality: quality,
    start_time: startTime,
    end_time: endTime,
    custom_filename: urls.length === 1 ? customFilename : (customFilename ? `${customFilename}_${idx + 1}` : ''),
    embed_thumbnail: embedThumb,
    embed_subs: embedSubs,
    title_hint: u.title_hint,
    thumbnail_hint: u.thumbnail_hint,
  }));

  try {
    const btn = document.getElementById('downloadBtn');
    btn.disabled = true;
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || '開始に失敗しました');
    }

    switchRightTab('queue');
    showToast(`${items.length}件のダウンロードをキューに追加しました`);
    await pollTasks();
  } catch (e) {
    showToast(e.message);
  } finally {
    document.getElementById('downloadBtn').disabled = false;
  }
}

function switchRightTab(tab) {
  rightTab = tab;
  document.getElementById('tabActiveQueue').classList.toggle('active', tab === 'queue');
  document.getElementById('tabHistory').classList.toggle('active', tab === 'history');
  document.getElementById('queueListContainer').classList.toggle('hidden', tab !== 'queue');
  document.getElementById('historyListContainer').classList.toggle('hidden', tab !== 'history');
  document.getElementById('clearQueueBtn').classList.toggle('hidden', tab !== 'queue');
  document.getElementById('clearHistoryBtn').classList.toggle('hidden', tab !== 'history');
}

async function pollTasks() {
  try {
    const res = await fetch('/api/tasks');
    if (!res.ok) return;
    const data = await res.json();

    const tasks = data.tasks || [];
    const history = data.history || [];

    document.getElementById('activeCountPill').textContent = tasks.length;
    document.getElementById('historyCountPill').textContent = history.length;

    const queueSig = JSON.stringify(tasks);
    if (queueSig !== lastQueueSig) {
      lastQueueSig = queueSig;
      renderTaskList('queueListContainer', tasks, true);
    }

    const historySig = JSON.stringify(history);
    if (historySig !== lastHistorySig) {
      lastHistorySig = historySig;
      renderTaskList('historyListContainer', history, false);
    }
  } catch (e) {
    // ignore transient poll errors
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function buildStatsHtml(t) {
  const progress = typeof t.progress === 'number' ? t.progress : 0;
  if (t.status === 'downloading') {
    return `<span>${escapeHtml(t.message || 'ダウンロード中')} (${escapeHtml(t.downloaded_str)} / ${escapeHtml(t.total_str)})</span><span>速度: ${escapeHtml(t.speed_str)} | 残り: ${escapeHtml(t.eta_str)}</span>`;
  } else if (t.status === 'completed') {
    return `<span style="color:#34d399;">✓ 保存完了 (${escapeHtml(t.file_size_str)})</span><span>${escapeHtml(t.completed_at || '')}</span>`;
  }
  return `<span class="status-msg">${escapeHtml(t.message || '')}</span><span>${progress.toFixed(0)}%</span>`;
}

function createTaskCardElement(t, isLiveQueue) {
  const card = document.createElement('div');
  const progress = typeof t.progress === 'number' ? t.progress : 0;
  const isRunning = ['queued', 'fetching_info', 'downloading', 'processing'].includes(t.status);
  const targetPath = t.file_path || t.save_dir || '';

  card.className = `task-item status-${t.status}`;
  card.dataset.id = t.id;

  card.innerHTML = `
    <div class="task-top">
      <img class="task-thumb" alt="thumb" />
      <div class="task-main">
        <div class="task-title"></div>
        <div class="task-sub">
          <span class="format-pill"></span>
          <span class="task-extractor"></span>
        </div>
      </div>
      <div class="task-actions">
        <button type="button" class="btn-action btn-cancel ${isRunning && isLiveQueue ? '' : 'hidden'}" data-action="cancel">中止</button>
        <button type="button" class="btn-action btn-open" data-action="open-folder">📁 保存先を開く</button>
      </div>
    </div>
    <div class="progress-wrap">
      <div class="progress-track">
        <div class="progress-fill" style="width: ${Math.max(2, progress)}%"></div>
      </div>
      <div class="progress-stats"></div>
    </div>
  `;

  const thumbImg = card.querySelector('.task-thumb');
  thumbImg.src = t.thumbnail || FALLBACK_THUMB;
  thumbImg.onerror = () => { thumbImg.src = FALLBACK_THUMB; };

  patchTaskCardElement(card, t, isLiveQueue);
  return card;
}

function patchTaskCardElement(card, t, isLiveQueue) {
  const progress = typeof t.progress === 'number' ? t.progress : 0;
  const isRunning = ['queued', 'fetching_info', 'downloading', 'processing'].includes(t.status);
  const targetPath = t.file_path || t.save_dir || '';

  card.className = `task-item status-${t.status}`;

  const thumbImg = card.querySelector('.task-thumb');
  const desiredThumb = t.thumbnail || FALLBACK_THUMB;
  if (thumbImg.dataset.srcRaw !== desiredThumb) {
    thumbImg.dataset.srcRaw = desiredThumb;
    thumbImg.src = desiredThumb;
  }

  const titleEl = card.querySelector('.task-title');
  titleEl.textContent = t.title || '';
  titleEl.title = t.title || '';

  card.querySelector('.format-pill').textContent = t.quality_label || t.container || '';
  card.querySelector('.task-extractor').textContent = t.extractor || '';

  const cancelBtn = card.querySelector('.btn-cancel');
  cancelBtn.dataset.taskId = t.id;
  cancelBtn.classList.toggle('hidden', !(isRunning && isLiveQueue));

  const openBtn = card.querySelector('.btn-open');
  openBtn.dataset.path = targetPath;

  card.querySelector('.progress-fill').style.width = `${Math.max(2, progress)}%`;
  card.querySelector('.progress-stats').innerHTML = buildStatsHtml(t);
}

function renderTaskList(containerId, items, isLiveQueue) {
  const container = document.getElementById(containerId);
  if (!items || items.length === 0) {
    container.innerHTML = isLiveQueue
      ? `<div class="empty-state"><p>現在進行中のタスクはありません</p><span>URLを入力して「ダウンロード開始」を押すと、ここにリアルタイムの進捗バー・速度・残り時間が表示されます。</span></div>`
      : `<div class="empty-state"><p>まだダウンロード履歴がありません</p></div>`;
    return;
  }

  // If existing cards match the exact ID sequence, patch in place to prevent flicker & click loss
  const existingCards = Array.from(container.children).filter(el => el.classList.contains('task-item'));
  const sameOrder =
    existingCards.length === items.length &&
    existingCards.every((el, idx) => el.dataset.id === items[idx].id);

  if (sameOrder) {
    items.forEach((t, idx) => patchTaskCardElement(existingCards[idx], t, isLiveQueue));
    return;
  }

  const frag = document.createDocumentFragment();
  items.forEach(t => {
    frag.appendChild(createTaskCardElement(t, isLiveQueue));
  });
  container.innerHTML = '';
  container.appendChild(frag);
}

async function cancelTask(taskId) {
  try {
    await fetch(`/api/cancel/${encodeURIComponent(taskId)}`, { method: 'POST' });
    showToast('キャンセルをリクエストしました');
    pollTasks();
  } catch (e) {
    showToast('キャンセルに失敗しました');
  }
}

async function clearFinishedTasks() {
  await fetch('/api/tasks/clear-finished', { method: 'DELETE' });
  pollTasks();
}

async function clearAllHistory() {
  await fetch('/api/history', { method: 'DELETE' });
  pollTasks();
  showToast('履歴をクリアしました');
}

let toastTimer = null;
function showToast(msg) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.add('hidden');
  }, 3500);
}
