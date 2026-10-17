package com.aiblackbox.portal.data.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TailnetAddressTest {

    @Test fun cgnat_range_classification() {
        assertTrue(TailnetAddress.isCgnatIpv4("100.64.0.1"))
        assertTrue(TailnetAddress.isCgnatIpv4("100.88.0.7"))
        assertTrue(TailnetAddress.isCgnatIpv4("100.127.255.255"))
        assertFalse(TailnetAddress.isCgnatIpv4("100.63.255.255"))   // just below
        assertFalse(TailnetAddress.isCgnatIpv4("100.128.0.1"))      // just above
        assertFalse(TailnetAddress.isCgnatIpv4("192.168.1.5"))      // LAN
        assertFalse(TailnetAddress.isCgnatIpv4("10.0.0.1"))         // private
        assertFalse(TailnetAddress.isCgnatIpv4("100.64.0"))         // too few octets
        assertFalse(TailnetAddress.isCgnatIpv4("not.an.ip.addr"))
        assertFalse(TailnetAddress.isCgnatIpv4(""))
    }

    @Test fun the_tailscale_interface_address_comes_first() {
        // On a cellular network the carrier can hand out a 100.64/10 (CGNAT) address too; the
        // Tailscale VPN interface (tun*) is the one the box reaches the phone by.
        val candidates = TailnetAddress.orderCandidates(listOf(
            "rmnet_data0" to "100.70.3.9",
            "wlan0" to "192.168.1.40",
            "tun0" to "100.88.0.7",
        ))
        assertEquals(listOf("100.88.0.7", "100.70.3.9"), candidates)
    }

    @Test fun without_a_tun_interface_every_cgnat_address_is_kept_in_order() {
        assertEquals(listOf("100.70.3.9", "100.71.0.1"), TailnetAddress.orderCandidates(listOf(
            "rmnet_data0" to "100.70.3.9", "lo" to "127.0.0.1", "rmnet_data1" to "100.71.0.1",
            "rmnet_data2" to "100.70.3.9")))
        assertEquals(emptyList<String>(), TailnetAddress.orderCandidates(listOf("wlan0" to "10.0.0.2")))
    }
}
