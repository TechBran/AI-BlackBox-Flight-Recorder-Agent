package com.aiblackbox.portal.data.remote

import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Discovers the device's OWN Tailscale tailnet IPv4 — a 100.64.0.0/10 (CGNAT) address
 * on one of its network interfaces. This is the join key the phone sends at attest
 * (control_phone): the phone can read its own interface address but NOT its tailnet
 * name, and the BlackBox mesh matches this IPv4 against `tailscale status` to resolve
 * + address the device. Returns null when the device is not on the tailnet.
 */
object TailnetAddress {

    /** True iff [ip] is in the Tailscale CGNAT range 100.64.0.0/10. PURE (JVM-testable). */
    fun isCgnatIpv4(ip: String): Boolean {
        val o = ip.trim().split(".")
        if (o.size != 4) return false
        val a = o[0].toIntOrNull() ?: return false
        val b = o[1].toIntOrNull() ?: return false
        if (o[2].toIntOrNull() == null || o[3].toIntOrNull() == null) return false
        return a == 100 && b in 64..127
    }

    /** The device's tailnet IPv4 from its interfaces, or null. Best-effort: any failure
     *  (no interfaces / security restriction) yields null and attest simply omits it. */
    fun localTailnetIpv4(): String? = try {
        NetworkInterface.getNetworkInterfaces().asSequence()
            .flatMap { it.inetAddresses.asSequence() }
            .filterIsInstance<Inet4Address>()
            .mapNotNull { it.hostAddress }
            .firstOrNull { isCgnatIpv4(it) }
    } catch (e: Exception) {
        null
    }
}
