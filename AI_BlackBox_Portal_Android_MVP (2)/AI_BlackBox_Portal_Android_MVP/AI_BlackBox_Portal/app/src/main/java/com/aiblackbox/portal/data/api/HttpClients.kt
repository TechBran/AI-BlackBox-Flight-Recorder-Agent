package com.aiblackbox.portal.data.api

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
 */
object HttpClients {
    val base: OkHttpClient by lazy { OkHttpClient.Builder().build() }
}
