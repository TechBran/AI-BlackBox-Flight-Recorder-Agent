/**
 * bubble-audio-player.js
 * The inline audio player the chat bubbles show: play/pause, a seekable
 * progress bar, the time and a download button.
 *
 * tts-stt.js had three ~100-line copies of it (audit P02-voice-web-12):
 * attachAudioPlayer (spoken replies), attachGeneratedAudioPlayer (generated
 * speech and music) and attachThinkingAudioPlayer (spoken reasoning). Each now
 * builds it here and keeps only where it goes and which button it lights up.
 *
 * Nothing here touches the page or the network: the caller places the
 * container, then calls play() for autoplay. Only an element in the page
 * reaches tts-stt.js's one-player-at-a-time guard (a capture-phase 'play'
 * listener on document).
 */

/**
 * m:ss for the player's clock; 0:00 before the duration is known.
 * @param {number} seconds
 * @returns {string}
 */
export function formatPlayerTime(seconds) {
    if (isNaN(seconds)) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Build one bubble audio player. Nothing is inserted: the caller places
 * `container`.
 * @param {object} opts
 * @param {string} opts.src - the audio (a data:/blob: URL or a server path)
 * @param {string} [opts.className] - classes after "bubble-audio-player"
 * @param {string} [opts.labelIcon] - a glyph before the play button (🧠 for reasoning)
 * @param {Function} [opts.onDownload] - the download button's action
 * @param {Function} [opts.onPlay] - also run when playback starts
 * @param {Function} [opts.onPause] - also run when playback pauses
 * @param {Function} [opts.onEnded] - also run when playback ends
 * @returns {{container: HTMLDivElement, audio: HTMLAudioElement, play: Function}}
 *          play() starts playback and logs (never throws) when the browser
 *          refuses autoplay.
 */
export function createBubbleAudioPlayer({
    src, className = '', labelIcon = '',
    onDownload = null, onPlay = null, onPause = null, onEnded = null,
}) {
    const container = document.createElement('div');
    container.className = className ? `bubble-audio-player ${className}` : 'bubble-audio-player';
    // Static markup only: the source goes in with setAttribute and the label
    // with textContent, so nothing a caller passes is parsed as HTML.
    container.innerHTML = `
        <audio preload="metadata"></audio>
        <button class="audio-play-btn" title="Play/Pause">
            <svg class="play-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            <svg class="pause-icon" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="display:none">
                <rect x="6" y="4" width="4" height="16"/>
                <rect x="14" y="4" width="4" height="16"/>
            </svg>
        </button>
        <div class="audio-progress-container">
            <div class="audio-progress-bar">
                <div class="audio-progress-fill"></div>
            </div>
        </div>
        <span class="audio-time">0:00</span>
        <button class="audio-download-btn" title="Download Audio">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
        </button>
    `;

    const audio = container.querySelector('audio');
    audio.setAttribute('src', src);
    if (labelIcon) {
        const label = document.createElement('span');
        label.className = 'audio-label-icon';
        label.textContent = labelIcon;
        audio.insertAdjacentElement('afterend', label);
    }

    const playBtn = container.querySelector('.audio-play-btn');
    const playIcon = container.querySelector('.play-icon');
    const pauseIcon = container.querySelector('.pause-icon');
    const progressBar = container.querySelector('.audio-progress-bar');
    const progressFill = container.querySelector('.audio-progress-fill');
    const timeDisplay = container.querySelector('.audio-time');

    const showPlaying = (playing) => {
        playIcon.style.display = playing ? 'none' : 'block';
        pauseIcon.style.display = playing ? 'block' : 'none';
        playBtn.classList.toggle('playing', playing);
    };

    playBtn.onclick = () => {
        if (audio.paused) audio.play();
        else audio.pause();
    };

    audio.onplay = () => {
        showPlaying(true);
        if (onPlay) onPlay();
    };

    audio.onpause = () => {
        showPlaying(false);
        if (onPause) onPause();
    };

    audio.onended = () => {
        showPlaying(false);
        progressFill.style.width = '0%';
        if (onEnded) onEnded();
    };

    audio.ontimeupdate = () => {
        if (audio.duration) {
            const percent = (audio.currentTime / audio.duration) * 100;
            progressFill.style.width = `${percent}%`;
            timeDisplay.textContent = formatPlayerTime(audio.currentTime);
        }
    };

    audio.onloadedmetadata = () => {
        timeDisplay.textContent = formatPlayerTime(audio.duration);
    };

    progressBar.onclick = (e) => {
        const rect = progressBar.getBoundingClientRect();
        const percent = (e.clientX - rect.left) / rect.width;
        audio.currentTime = percent * audio.duration;
    };

    container.querySelector('.audio-download-btn').onclick = () => {
        if (onDownload) onDownload();
    };

    const play = () => audio.play().catch(e => console.log("Autoplay prevented by browser:", e));

    return { container, audio, play };
}
