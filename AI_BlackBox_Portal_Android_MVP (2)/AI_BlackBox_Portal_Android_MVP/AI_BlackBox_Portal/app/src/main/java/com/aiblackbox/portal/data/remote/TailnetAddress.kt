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

    /**
     * PURE: the 100.64.0.0/10 addresses among `(interface name, IPv4)` pairs, the Tailscale VPN
     * interface (`tun*`) first, otherwise in interface order, without duplicates. That range is
     * also carrier-grade-NAT space, so a phone on a cellular network can hold a second address
     * in it that is not its tailnet one.
     */
    fun orderCandidates(addresses: List<Pair<String, String>>): List<String> =
        addresses.filter { isCgnatIpv4(it.second) }
            .sortedBy { if (it.first.startsWith("tun", ignoreCase = true)) 0 else 1 }
            .map { it.second.trim() }
            .distinct()

    /** Every candidate tailnet IPv4 on this device ([orderCandidates]). Best-effort: any
     *  failure (no interfaces / security restriction) yields an empty list. */
    fun localTailnetIpv4Candidates(): List<String> = try {
        orderCandidates(NetworkInterface.getNetworkInterfaces().asSequence()
            .flatMap { nif ->
                nif.inetAddresses.asSequence().filterIsInstance<Inet4Address>()
                    .mapNotNull { addr -> addr.hostAddress?.let { nif.name.orEmpty() to it } }
            }
            .toList())
    } catch (e: Exception) {
        emptyList()
    }

    /** The device's tailnet IPv4 from its interfaces (the Tailscale interface's when there is
     *  one), or null. Best-effort: any failure yields null and attest simply omits it. */
    fun localTailnetIpv4(): String? = localTailnetIpv4Candidates().firstOrNull()
}
