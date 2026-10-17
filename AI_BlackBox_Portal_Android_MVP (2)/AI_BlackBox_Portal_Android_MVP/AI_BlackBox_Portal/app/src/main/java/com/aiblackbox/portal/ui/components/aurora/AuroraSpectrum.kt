package com.aiblackbox.portal.ui.components.aurora

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * The analysis window applied before the transform.
 *
 * [RECTANGULAR] is not a style option — it is the STATUS QUO, kept so the choice of [HANN] can be
 * measured against it rather than asserted. It is what the Goertzel probes next door effectively
 * use (they simply run over the chunk), and `Hann earns its place` in AuroraSpectrumTest is the
 * A/B that documents why the FFT path does not.
 */
enum class SpectrumWindow { RECTANGULAR, HANN }

/**
 * Radix-2 real FFT — the transform only, no windowing, no calibration, no frequency map.
 *
 * Real input of length [size] is transformed as a HALF-length complex FFT (even samples into the
 * real part, odd into the imaginary) and then split back apart, which is the standard way to get a
 * real transform for half the butterflies and half the working memory. The butterflies run IN PLACE
 * over scratch this instance owns, and every twiddle, the bit-reversal table and the output buffer
 * are built once in the constructor: calling [magnitudes] allocates NOTHING. That is a requirement
 * rather than an optimisation — the caller is a per-frame audio or render path, where a fresh
 * FloatArray per call is garbage the ribbon pays for.
 *
 * Pure Kotlin for the same reason [AuroraAnalyser] is (no android.*, no Compose, no DSP
 * dependency): the interesting part is arithmetic, and arithmetic should be testable on the JVM.
 *
 * NOT thread-safe — the scratch IS the instance. One instance per audio stream, exactly as
 * [AuroraAnalyser] documents for itself.
 */
class AuroraFft(val size: Int) {

    init {
        require(size >= 4) { "FFT size must be at least 4, was $size" }
        require(size and (size - 1) == 0) { "FFT size must be a power of two, was $size" }
    }

    /** The complex transform actually performed: real input of [size] packs into [half] points. */
    private val half = size / 2

    private val re = FloatArray(half)
    private val im = FloatArray(half)

    /** Decimation-in-time consumes its input bit-reversed; precomputed so no call has to. */
    private val bitRev = IntArray(half)

    private val twCos = FloatArray(half / 2)
    private val twSin = FloatArray(half / 2)

    /** e^(-2*pi*i*k/size) for the split step that unpacks the half-length complex result. */
    private val unpackCos = FloatArray(half)
    private val unpackSin = FloatArray(half)

    /** Owned output: |X[k]| for k = 0..size/2, overwritten by every call. */
    private val mag = FloatArray(half + 1)

    init {
        var bits = 0
        while ((1 shl bits) < half) bits++
        for (j in 0 until half) {
            var r = 0
            for (b in 0 until bits) if ((j shr b) and 1 == 1) r = r or (1 shl (bits - 1 - b))
            bitRev[j] = r
        }
        for (t in twCos.indices) {
            val a = -2.0 * PI * t / half
            twCos[t] = cos(a).toFloat()
            twSin[t] = sin(a).toFloat()
        }
        for (k in unpackCos.indices) {
            val a = -2.0 * PI * k / size
            unpackCos[k] = cos(a).toFloat()
            unpackSin[k] = sin(a).toFloat()
        }
    }

    /**
     * Magnitude spectrum of the first [size] samples of [frame], as `size/2 + 1` bins from DC to
     * Nyquist. Unscaled — a window's gain and any amplitude calibration belong to the caller that
     * chose them ([AuroraSpectrum]).
     *
     * @return this instance's own buffer. Read it or copy it before the next call.
     */
    fun magnitudes(frame: FloatArray): FloatArray {
        require(frame.size >= size) { "frame is ${frame.size}, need $size" }

        // Pack even/odd into one complex sequence, bit-reversed on the way in.
        for (j in 0 until half) {
            val r = bitRev[j]
            re[j] = frame[2 * r]
            im[j] = frame[2 * r + 1]
        }

        var len = 2
        while (len <= half) {
            val hl = len shr 1
            val step = half / len
            var i = 0
            while (i < half) {
                var j = 0
                while (j < hl) {
                    val t = j * step
                    val wr = twCos[t]
                    val wi = twSin[t]
                    val a = i + j
                    val b = a + hl
                    val br = re[b]
                    val bi = im[b]
                    val xr = br * wr - bi * wi
                    val xi = br * wi + bi * wr
                    re[b] = re[a] - xr
                    im[b] = im[a] - xi
                    re[a] += xr
                    im[a] += xi
                    j++
                }
                i += len
            }
            len = len shl 1
        }

        // Split step. Z is the half-length transform of (even + i*odd); the real transform's bin k
        // is Fe[k] + e^(-2*pi*i*k/size) * Fo[k], where Fe/Fo are the even/odd sequences recovered
        // from Z[k] and conj(Z[half-k]). DC and Nyquist fall out of Z[0] alone.
        val z0r = re[0]
        val z0i = im[0]
        mag[0] = abs(z0r + z0i)
        mag[half] = abs(z0r - z0i)
        for (k in 1 until half) {
            val kr = re[k]
            val ki = im[k]
            val cr = re[half - k]
            val ci = -im[half - k]
            val fer = 0.5f * (kr + cr)
            val fei = 0.5f * (ki + ci)
            // Fo = (Z[k] - conj(Z[half-k])) / 2i, and dividing by i is multiplying by -i.
            val dr = 0.5f * (kr - cr)
            val di = 0.5f * (ki - ci)
            val for_ = di
            val foi = -dr
            val wr = unpackCos[k]
            val wi = unpackSin[k]
            val xr = fer + (for_ * wr - foi * wi)
            val xi = fei + (for_ * wi + foi * wr)
            mag[k] = sqrt(xr * xr + xi * xi)
        }
        return mag
    }
}

/**
 * A Hann-windowed FFT folded onto [AuroraAnalyser.BINS] log-spaced frequency bins — a SECOND,
 * independent reading of the same audio the four Goertzel bands already describe.
 *
 * WHY A SECOND CHANNEL AND NOT A WIDER FIRST ONE. [AuroraAnalyser.BANDS] is four single-bin
 * Goertzel probes whose every constant — gains, peak seeds, noise floors, headroom, gamma — was
 * measured on-device against those exact four probes, and AuroraVoice is exactly four wide.
 * Widening it would invalidate the calibration and the renderer at once. This channel is additive:
 * it computes its own numbers from its own transform and shares nothing but the sample rate and the
 * Nyquist rule.
 *
 * WHAT A BIN MEANS. Bins are geometric — equal MUSICAL width, ~1.21x per bin — because that is how
 * pitch is heard and how a spectral display has to be laid out to look like the sound. The reading
 * is `sqrt(band power / window ENBW)`, which is amplitude-calibrated: a full-scale sine sitting in
 * a bin reads ~1.0 at any sample rate, any FFT size and either window, so a consumer can be written
 * against one operating point instead of a per-rate scale factor. Broadband content spreads across
 * bins by real energy, so wide (high) bins do legitimately collect more of it than narrow (low)
 * ones — that is the truth about the signal, and any perceptual weighting on top of it is the
 * consumer's decision, not this layer's.
 *
 * Pure Kotlin, no android.*, no dependency, no per-call allocation, NOT thread-safe — one instance
 * per audio stream, for the reasons [AuroraAnalyser] gives under D1.
 *
 * @param sampleRate the stream's real rate in Hz.
 * @param fftSize normally derived from the rate by [fftSizeFor]; a parameter only so tests can hold
 *   it fixed while varying the window.
 */
class AuroraSpectrum(
    val sampleRate: Int,
    val fftSize: Int = fftSizeFor(sampleRate),
    val window: SpectrumWindow = SpectrumWindow.HANN,
) {

    init {
        require(sampleRate > 0) { "sampleRate must be positive, was $sampleRate" }
        require(fftSize >= 4 && fftSize and (fftSize - 1) == 0) {
            "fftSize must be a power of two >= 4, was $fftSize"
        }
    }

    /** Spacing between adjacent FFT bins. The floor on what any log bin can resolve. */
    val fftResolutionHz: Float = sampleRate.toFloat() / fftSize

    private val fft = AuroraFft(fftSize)

    /** The windowed frame, refilled in place. */
    private val frame = FloatArray(fftSize)

    private val coeff = FloatArray(fftSize) { n ->
        when (window) {
            SpectrumWindow.RECTANGULAR -> 1f
            // Periodic (not symmetric) Hann — the form that belongs in a transform, since the
            // frame is treated as one period of a repeating signal.
            SpectrumWindow.HANN -> (0.5 - 0.5 * cos(2.0 * PI * n / fftSize)).toFloat()
        }
    }

    /**
     * Amplitude calibration: `1 / (fftSize * coherentGain)`, so an on-bin sine of amplitude A reads
     * A (after the x2 that folds in its negative-frequency twin). Dividing out the window's
     * coherent gain is what makes a Hann reading directly comparable to a rectangular one.
     */
    private val magScale: Float

    /**
     * Equivalent noise bandwidth of the window, in FFT bins (1.0 rectangular, 1.5 Hann). A window
     * spreads one tone over several bins, so summing power across a band over-counts by exactly
     * this factor; dividing it back out is what keeps the calibration window-independent. Measured
     * from the coefficients rather than written down, so the two can never disagree.
     */
    private val enbw: Float

    /** Owned outputs, overwritten per call. */
    private val mags = FloatArray(fftSize / 2 + 1)
    private val bins = FloatArray(AuroraAnalyser.BINS)

    /** Half-open FFT-bin range owned by each log bin. Contiguous, non-overlapping, may be empty. */
    private val lo = IntArray(AuroraAnalyser.BINS)
    private val hi = IntArray(AuroraAnalyser.BINS)

    init {
        var sum = 0.0
        var sumSq = 0.0
        for (c in coeff) {
            sum += c
            sumSq += c.toDouble() * c
        }
        val coherentGain = sum / fftSize
        magScale = (1.0 / (fftSize * coherentGain)).toFloat()
        enbw = ((sumSq / fftSize) / (coherentGain * coherentGain)).toFloat()

        // The SAME Nyquist rule the Goertzel probes apply (AuroraAnalyser.NYQUIST_MARGIN): content
        // this close to Nyquist cannot be measured, only aliased. Bins beyond it own nothing and
        // read a flat zero — an 8 kHz mu-law telephony stream therefore loses the top of the ribbon
        // rather than sparkling at folded-over vowels.
        val cap = minOf(
            fftSize / 2 + 1,
            ceil(AuroraAnalyser.NYQUIST_MARGIN.toDouble() * sampleRate / fftResolutionHz).toInt(),
        )
        for (b in 0 until AuroraAnalyser.BINS) {
            // Bin 0 of the FFT is DC and belongs to no log bin.
            val l = minOf(cap, maxOf(1, fftBinAtOrAbove(edgeHz(b))))
            val h = minOf(cap, fftBinAtOrAbove(edgeHz(b + 1)))
            lo[b] = l
            hi[b] = maxOf(l, h)
        }
    }

    private fun fftBinAtOrAbove(hz: Float): Int = ceil(hz.toDouble() / fftResolutionHz).toInt()

    /** How many FFT bins log bin [bin] owns. Zero above the Nyquist margin. */
    fun fftBinCount(bin: Int): Int = hi[bin] - lo[bin]

    /** A representative FFT bin inside log bin [bin] — its middle. */
    fun fftBinFor(bin: Int): Int = (lo[bin] + hi[bin] - 1) / 2

    fun fftBinCentreHz(fftBin: Int): Float = fftBin * fftResolutionHz

    /**
     * Amplitude-calibrated magnitude spectrum of [len] samples from [offset], as `fftSize/2 + 1`
     * bins from DC to Nyquist. Mostly of interest to tests and diagnostics — consumers want
     * [analyze].
     *
     * A run longer than the window is analysed from its MOST RECENT [fftSize] samples: a visualizer
     * wants now, not the average of everything the caller has buffered. A shorter run is zero-padded
     * and therefore reads quieter, in proportion to how much of the window it fills — a caller whose
     * chunks are shorter than the window should overlap them rather than accept the attenuation.
     *
     * @return this instance's own buffer.
     */
    fun magnitudes(
        samples: FloatArray,
        offset: Int = 0,
        len: Int = samples.size - offset,
    ): FloatArray {
        val off = offset.coerceIn(0, samples.size)
        val n = len.coerceIn(0, samples.size - off)
        val take = minOf(n, fftSize)
        val start = off + n - take
        for (i in 0 until take) frame[i] = samples[start + i] * coeff[i]
        frame.fill(0f, take, fftSize)

        val raw = fft.magnitudes(frame)
        val half = fftSize / 2
        for (k in 0..half) {
            // Every bin but DC and Nyquist has a negative-frequency twin carrying half the energy.
            val twin = if (k == 0 || k == half) 1f else 2f
            mags[k] = raw[k] * magScale * twin
        }
        return mags
    }

    /**
     * [AuroraAnalyser.BINS] log-spaced band readings of [len] samples from [offset], each the
     * amplitude-calibrated RMS level of the content in that band.
     *
     * @return this instance's own buffer.
     */
    fun analyze(
        samples: FloatArray,
        offset: Int = 0,
        len: Int = samples.size - offset,
    ): FloatArray {
        magnitudes(samples, offset, len)
        for (b in 0 until AuroraAnalyser.BINS) {
            var power = 0.0
            for (k in lo[b] until hi[b]) {
                val m = mags[k].toDouble()
                power += m * m
            }
            bins[b] = sqrt(power / enbw).toFloat()
        }
        return bins
    }

    companion object {

        /**
         * Bottom of the map. Below the lowest voiced pitch a phone mic meaningfully carries, and
         * deliberately ABOVE 60 Hz so mains hum and its immediate skirt never own a bin of their
         * own. Lowering it is not free: the bottom bin is only ~17 Hz wide, and a bin narrower than
         * [FFT_WINDOW_SEC] can resolve stops owning an FFT bin of its own — see the
         * `every log bin owns at least one FFT bin` test, which is what fails first if this moves.
         */
        internal const val MIN_HZ = 80f

        /**
         * The lowest rate the map has to be FULLY defined at (16 kHz — the smallest any of our
         * voice backends produces; see AuroraAnalyser D2). Lower rates are legal and simply lose
         * their top bins.
         */
        internal const val MAP_RATE_HZ = 16000f

        /**
         * Top of the map = the analyser's own Nyquist rule evaluated at [MAP_RATE_HZ], i.e. 7200 Hz.
         * DERIVED, not chosen: any fixed top edge above this would be unmeasurable at 16 kHz, so the
         * bins there would differ by rate and the map would stop meaning one thing. It lands just
         * above the 6800 Hz sibilance probe, so the spectrum channel covers everything the band
         * channel does.
         */
        internal const val MAX_HZ = AuroraAnalyser.NYQUIST_MARGIN * MAP_RATE_HZ

        /**
         * Target analysis window LENGTH IN TIME, which is what actually fixes frequency resolution
         * — 60 ms buys ~17 Hz, and the bottom log bin is ~17 Hz wide. Sizing from time rather than
         * fixing a sample count is what makes 16k/24k/44.1k/48k resolve the same map: a fixed 1024
         * points would be 64 ms at 16 kHz and 21 ms at 48 kHz, and the low bins would collapse into
         * each other at the top rates.
         *
         * 60 ms, not 64: no supported rate multiplies it into an exact power of two, so the
         * round-up in [fftSizeFor] never sits on a floating-point knife edge (16000 * 0.064f is
         * 1024.00006 in float, which would round UP to 2048 and silently double the window).
         *
         * This is the WINDOW, not the update interval. A consumer wanting faster motion slides this
         * window over a shorter hop rather than shortening it — shortening it costs low-frequency
         * resolution that cannot be bought back.
         */
        internal const val FFT_WINDOW_SEC = 0.06f

        private const val MIN_FFT_SIZE = 256

        /** Ceiling for an implausible rate, so nothing ever asks for a 16k-point transform. */
        private const val MAX_FFT_SIZE = 8192

        /**
         * Smallest power-of-two transform covering [FFT_WINDOW_SEC] at [sampleRate]:
         * 16k -> 1024 (64 ms), 24k -> 2048 (85 ms), 44.1k -> 4096 (93 ms), 48k -> 4096 (85 ms).
         */
        fun fftSizeFor(sampleRate: Int): Int {
            require(sampleRate > 0) { "sampleRate must be positive, was $sampleRate" }
            val target = sampleRate * FFT_WINDOW_SEC.toDouble()
            var n = MIN_FFT_SIZE
            while (n < target && n < MAX_FFT_SIZE) n = n shl 1
            return n
        }

        private val DECADES = ln((MAX_HZ / MIN_HZ).toDouble())

        /** Lower edge of log bin [edge], in Hz; `edge == BINS` is the top of the map. */
        internal fun edgeHz(edge: Int): Float =
            (MIN_HZ * exp(DECADES * edge / AuroraAnalyser.BINS)).toFloat()

        /** Geometric centre of log bin [bin] — the frequency it is "about". */
        internal fun centreHz(bin: Int): Float =
            (MIN_HZ * exp(DECADES * (bin + 0.5) / AuroraAnalyser.BINS)).toFloat()
    }
}
