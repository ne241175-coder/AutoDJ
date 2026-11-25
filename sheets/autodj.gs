/**
 * AutoDJ 用の Apps Script（「ジャンル横断」シートに入れる）。
 *
 * やること
 *   1. 曲リスト: YouTube の URL だけ入っていて曲名が空の行 → 動画のタイトルから曲名を入れる（「曲名/アーティスト」）
 *   2. 構成: 曲リストにあって構成タブにまだ無い曲 → 構成タブの一番下に、その曲の枠（曲名＋ラベル＋終了の行）を足す
 *      人は「開始」の時刻を入れて、ラベルを直すだけ
 *
 * 入れ方（最初の1回だけ）
 *   シートの メニュー「拡張機能」→「Apps Script」→ このファイルの中身を貼って保存 →
 *   上の関数の選択で「setup」を選んで ▶ 実行 → Google の許可画面で許可する
 *   以降は 曲リストを編集したとき と 10 分ごと に自動で動く。シートのメニュー「AutoDJ」からも動かせる
 */

const SONGS = '曲リスト';
const STRUCT = '構成';
const COL_TITLE = 1;     // 曲リスト A: 曲名
const COL_URL = 7;       // 曲リスト G: YouTube
const COL_EFFECTIVE = 13; // 構成 M: 曲（自動）
const MAX_ROW = 4836;     // 構成タブの式が入っている最後の行（式の範囲を広げたらここも変える）
const AUTO_NOTE = '動画のタイトル「';
const TEMPLATE = ['イントロ', 'A', 'B', 'サビ', '間奏', 'A', 'B', 'サビ', 'Cメロ', '落ちサビ', 'サビ', 'アウトロ', '終了'];

function onOpen() {
  SpreadsheetApp.getUi().createMenu('AutoDJ')
    .addItem('曲名と構成の枠を今すぐ作る', 'autodjSync')
    .addItem('自動で動くようにする（最初に1回）', 'setup')
    .addToUi();
}

function setup() {
  const ss = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers()
    .filter((t) => ['autodjOnEdit', 'autodjSync'].includes(t.getHandlerFunction()))
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('autodjOnEdit').forSpreadsheet(ss).onEdit().create();
  ScriptApp.newTrigger('autodjSync').timeBased().everyMinutes(10).create();
  autodjSync();
  ss.toast('曲リストを編集すると、曲名と構成の枠が自動で入ります', 'AutoDJ', 8);
}

function autodjOnEdit(e) {
  if (e && e.range && e.range.getSheet().getName() !== SONGS) return;
  autodjSync(e);
}

function autodjSync(e) {
  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(20000)) return;
  try {
    if (e && e.range) renameBlock_(e);
    const titled = fillTitles_();
    const added = addBlocks_();
    if (titled + added > 0) {
      SpreadsheetApp.getActive().toast(
        (titled ? `曲名を ${titled} 曲入れました。` : '') + (added ? `構成に ${added} 曲の枠を足しました。` : ''), 'AutoDJ', 6);
    }
  } finally {
    lock.releaseLock();
  }
}

// ---------- 1. 曲名 ----------
function fillTitles_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(SONGS);
  const n = sh.getLastRow() - 1;
  if (n < 1) return 0;
  const vals = sh.getRange(2, 1, n, COL_URL).getValues();
  const names = new Set(vals.map((r) => String(r[0]).trim()).filter(Boolean));
  let count = 0;
  vals.forEach((r, i) => {
    const title = String(r[COL_TITLE - 1]).trim();
    const url = String(r[COL_URL - 1]).trim();
    if (title || !url) return;
    const info = oembed_(url);
    if (!info) return;
    let name = guessName_(info.title, info.author_name);
    if (!name) return;
    const dup = names.has(name);
    if (dup) name = `${name}（2）`;
    const cell = sh.getRange(i + 2, COL_TITLE);
    cell.setValue(name);
    cell.setNote(`${AUTO_NOTE}${info.title}」から自動で入れました。違っていたら直してください` +
      (dup ? '（同じ曲名がもうあります。同じ曲なら、この行は消してください）' : ''));
    names.add(name);
    count++;
  });
  return count;
}

function oembed_(url) {
  try {
    const r = UrlFetchApp.fetch('https://www.youtube.com/oembed?format=json&url=' + encodeURIComponent(url),
      { muteHttpExceptions: true });
    return r.getResponseCode() === 200 ? JSON.parse(r.getContentText()) : null;
  } catch (e) {
    return null;
  }
}

const NOISE_ = /(official|music\s*video|lyric|lyrics|audio|video|\bmv\b|\bm\/v\b|公式|歌詞|full|ver\.?|version|hd|4k|live|teaser|short)/i;

function cleanArtist_(s) {
  return String(s || '')
    .replace(/\s*-\s*Topic$/, '')
    .replace(/\s*(official\s*(youtube)?\s*(channel)?|オフィシャル\s*(youtube)?\s*(チャンネル)?|公式\s*(youtube)?\s*(チャンネル)?|channel|vevo)\s*$/i, '')
    .replace(/^[\s\-_・]+|[\s\-_・]+$/g, '');
}

function norm_(s) {
  return String(s || '').replace(/[\s・.\-_'’]/g, '').toLowerCase();
}

/** 動画タイトル → 「曲名/アーティスト」（AutoDJ の backend/addsong.py の guess_title と同じ考え方） */
function guessName_(rawTitle, channel) {
  const artist = cleanArtist_(channel);
  let t = String(rawTitle || '');
  const join = (song, who) => (who && norm_(who) !== norm_(song) ? `${song.trim()}/${who.trim()}` : song.trim());
  // 米津玄師 MV「LOSER」 / BLACKPINK - ‘JUMP’ M/V / TWICE "What is Love?" M/V（' は SOUL'd OUT などに入るので使わない）
  const m = t.match(/[「『‘“"](.+?)[」』’”"]/);
  if (m) {
    const before = t.slice(0, m.index).replace(/(【[^】]*】|\bmv\b|\bm\/v\b|music\s*video)/gi, '')
      .replace(/^[\s\-–—/／|｜]+|[\s\-–—/／|｜]+$/g, '');
    return join(m[1], before || artist);
  }
  t = t.replace(/[【\[［(（<〈][^】\]］)）>〉]*[】\]］)）>〉]/g, (x) => (NOISE_.test(x) || x.length <= 4 ? '' : x));
  t = t.replace(/\b(official\s+)?(music\s+video|lyric\s+video|m\/v|mv|audio)\b/gi, '')
    .replace(/^[\s\-–—/／|｜]+|[\s\-–—/／|｜]+$/g, '');
  const parts = t.split(/\s+[-–—/／|｜]\s+|\s*[／｜]\s*/).map((p) => p.trim()).filter(Boolean);
  if (parts.length >= 2) {
    const same = parts.map((p) => !!norm_(p) && (norm_(channel).includes(norm_(p)) || norm_(p).includes(norm_(channel)) ||
      norm_(p) === norm_(artist)));
    if (same.some((x) => x) && !same.every((x) => x)) {
      return join(parts[same.indexOf(false)], parts[same.indexOf(true)]);
    }
    return join(parts[parts.length - 1], artist || parts[0]);
  }
  return join(t || String(rawTitle || ''), artist);
}

// ---------- 2. 構成の枠 ----------
function addBlocks_() {
  const ss = SpreadsheetApp.getActive();
  const songs = ss.getSheetByName(SONGS);
  const st = ss.getSheetByName(STRUCT);
  const ns = songs.getLastRow() - 1;
  if (ns < 1) return 0;
  const want = songs.getRange(2, COL_TITLE, ns, 1).getValues().map((r) => String(r[0]).trim()).filter(Boolean);
  const nrow = st.getMaxRows() - 1;
  const input = st.getRange(2, 1, nrow, 3).getValues();             // A〜C（人が入れるところ）
  const have = new Set(st.getRange(2, COL_EFFECTIVE, nrow, 1).getValues().map((r) => String(r[0]).trim()));
  input.forEach((r) => { if (String(r[0]).trim()) have.add(String(r[0]).trim()); });
  let last = 0;                                                      // A〜C に何か入っている最後の行（0 始まり）
  input.forEach((r, i) => { if (r.some((c) => String(c).trim() !== '')) last = i + 1; });
  const todo = want.filter((t, i) => !have.has(t) && want.indexOf(t) === i);
  if (!todo.length) return 0;
  const rows = [];
  todo.forEach((t) => TEMPLATE.forEach((label, k) => rows.push([k === 0 ? t : '', label, ''])));
  const start = last + 2;                                            // シートの行番号
  const limit = Math.min(MAX_ROW, st.getMaxRows());
  if (start + rows.length - 1 > limit) {
    ss.toast(`構成タブが ${limit} 行をこえるので、枠を足せませんでした（行と式の範囲を広げてください）`, 'AutoDJ', 10);
    return 0;
  }
  st.getRange(start, 1, rows.length, 3).setValues(rows);
  return todo.length;
}

// ---------- 3. 曲名を直したとき ----------
/** 曲リストの曲名を手で直したら、構成タブの枠の曲名も同じに直す（古い枠が残って、新しい枠が二重にできないように）。
 *  自動で入れた曲名を直したときは、「自動で入れました」のメモも消す。1つのセルを直したときだけ（貼り付けでは動かない） */
function renameBlock_(e) {
  const r = e.range;
  if (r.getSheet().getName() !== SONGS || r.getColumn() !== COL_TITLE || r.getNumRows() !== 1 || r.getNumColumns() !== 1 ||
      r.getRow() < 2) return;
  const before = String(e.oldValue || '').trim();
  const after = String(e.value || '').trim();
  if (before === after) return;
  if (String(r.getNote() || '').startsWith(AUTO_NOTE)) r.clearNote();
  if (!before || !after) return;
  const st = SpreadsheetApp.getActive().getSheetByName(STRUCT);
  const a = st.getRange(2, 1, st.getMaxRows() - 1, 1).getValues().map((x) => String(x[0]).trim());
  if (a.includes(after)) return;                                     // 新しい名前の枠がもうある
  a.forEach((x, i) => { if (x === before) st.getRange(i + 2, 1).setValue(after); });   // 前からある枠は全行に曲名が入っている
}
