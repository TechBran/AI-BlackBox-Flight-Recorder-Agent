package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.wellness.WellnessChatSigning
import okhttp3.Dispatcher
import okhttp3.OkHttpClient

/**
 * The one OkHttp client of the process (audit A10-data-remote-06, A02-overlay-service-10,
 * A01-activities-root-07, rank 76).
 *
 * Every client in the app derives from [base] with `base.newBuilder()`, which shares its
 * connection pool and dispatcher and changes only what the caller sets (timeouts, an
 * interceptor, a ping interval). Each BlackBoxApi used to build three independent clients,
 * the app builds a BlackBoxApi in about 27 places, and the overlay, the WebView activity and
 * the terminal sockets built more per call, so every screen paid its own TCP+TLS handshake to
 * the same box and left its own executor threads and idle pool behind.
 *
 * [base] carries OkHttp's default timeouts, which is what each `OkHttpClient.Builder()` it
 * replaced started from, so a derived client times out exactly as before.
 *
 * Nothing may shut down [base]'s dispatcher or evict its pool: the whole app runs on them.
 *
 * The one dispatcher's limits are app-wide, so they are raised from OkHttp's defaults (64
 * calls in all, 5 per host). Those defaults were per client while every screen built its
 * own; on one shared dispatcher, 5 per host would let a chat stream plus four slow calls on
 * other screens queue every further call to the box, the CU STOP among them. An SSE stream
 * holds its slot for the whole turn (SSEClient reads it inside onResponse), and an open
 * WebSocket holds one of [MAX_REQUESTS] (not of the per-host limit) while it is open.
 *
 * (M7) [base] carries the Health chat-signing interceptor ([WellnessChatSigning]), so every
 * derived client signs the allow-listed chat and health-read requests of the enrolled phone
 * (spec §6.1 "Chat") and leaves every other request exactly as it was.
 */
object HttpClients {
    /** Concurrent async calls to one host (the box) across the whole app. */
    const val MAX_REQUESTS_PER_HOST = 64

    /** Concurrent async calls in all, open WebSockets included. */
    const val MAX_REQUESTS = 2 * MAX_REQUESTS_PER_HOST

    val base: OkHttpClient by lazy {
        val dispatcher = Dispatcher().apply {
            maxRequests = MAX_REQUESTS
            maxRequestsPerHost = MAX_REQUESTS_PER_HOST
        }
        OkHttpClient.Builder()
            .dispatcher(dispatcher)
            .addInterceptor(WellnessChatSigning.interceptor)
            .build()
    }
}
