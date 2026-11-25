package io.github.vip3rousmango.couchchd.receiver;

import java.io.IOException;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Answers only CouchCHD presence messages. An invitation is a UI hint, never a connection. */
final class Presence implements AutoCloseable {
    private static final int PORT = 42847;
    private static final long TTL = TimeUnit.SECONDS.toNanos(12);
    private static final Pattern QUERY = Pattern.compile("CouchCHD/2 (?:find ([0-9a-f]{32})|invite ([0-9a-f]{32}) ([0-9a-f]{32}))");
    private DatagramSocket listening;
    private boolean closed;

    static Presence start(String model, Consumer<String> invite) {
        Presence presence = new Presence();
        Thread worker = new Thread(() -> presence.listen(model, invite), "CouchCHD nearby");
        worker.setDaemon(true);
        worker.start();
        return presence;
    }

    @Override public synchronized void close() {
        closed = true;
        if (listening != null) listening.close();
    }

    private static int number(InetAddress address) {
        byte[] b = address.getAddress();
        return (b[0] & 255) << 24 | (b[1] & 255) << 16 | (b[2] & 255) << 8 | b[3] & 255;
    }

    private static List<InterfaceAddress> links() throws SocketException {
        List<InterfaceAddress> result = new ArrayList<>();
        Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
        if (interfaces == null) return result;
        while (interfaces.hasMoreElements()) {
            NetworkInterface link = interfaces.nextElement();
            if (!link.isUp() || link.isLoopback() || link.isPointToPoint()) continue;
            for (InterfaceAddress a : link.getInterfaceAddresses()) {
                InetAddress local = a.getAddress();
                if (local instanceof Inet4Address && a.getBroadcast() != null && a.getNetworkPrefixLength() > 0 &&
                        (local.isSiteLocalAddress() || local.isLinkLocalAddress())) result.add(a);
            }
        }
        return result;
    }

    private static boolean onLink(InetAddress from, List<InterfaceAddress> links) {
        if (!(from instanceof Inet4Address) || !(from.isSiteLocalAddress() || from.isLinkLocalAddress())) return false;
        for (InterfaceAddress a : links) {
            int mask = -1 << (32 - a.getNetworkPrefixLength());
            if (((number(from) ^ number(a.getAddress())) & mask) == 0 && !from.equals(a.getBroadcast())) return true;
        }
        return false;
    }

    private void listen(String model, Consumer<String> invite) {
        String label = Rules.deviceLabel(model);
        byte[] secret = new byte[16]; new SecureRandom().nextBytes(secret);
        String cookie = Rules.hex(secret);
        String suffix = " " + cookie + " " + (label.isEmpty() ? "Android handheld" : label);
        byte[] buf = new byte[192];
        DatagramPacket p = new DatagramPacket(buf, buf.length);
        Map<String,Long> seen = new HashMap<>();
        List<InterfaceAddress> links = Collections.emptyList();
        long nextLinks = System.nanoTime(), nextInvite = nextLinks;
        try (DatagramSocket s = new DatagramSocket(null)) {
            synchronized (this) {
                if (closed) return;
                s.bind(new InetSocketAddress(PORT)); listening = s;
            }
            for (;;) {
                p.setLength(buf.length); s.receive(p);
                Matcher m = QUERY.matcher(new String(buf, 0, p.getLength(), StandardCharsets.US_ASCII));
                if (!m.matches()) continue;
                long now = System.nanoTime();
                if (now - nextLinks >= 0) { links = links(); nextLinks = now + TTL; }
                if (!onLink(p.getAddress(), links)) continue;
                String ip = p.getAddress().getHostAddress();
                boolean find = m.group(1) != null;
                String nonce = find ? m.group(1) : m.group(2);
                String key = ip + ":" + p.getPort() + ":" + nonce;
                seen.entrySet().removeIf(e -> now - e.getValue() >= TTL);
                if (find) {
                    Long last = seen.get(key);
                    if (last == null && seen.size() >= 24 || last != null && now-last < TimeUnit.SECONDS.toNanos(1)) continue;
                    seen.put(key, now);
                    byte[] data = ("CouchCHD/2 here " + nonce + suffix).getBytes(StandardCharsets.US_ASCII);
                    s.send(new DatagramPacket(data,data.length,p.getSocketAddress()));
                } else if (cookie.equals(m.group(3)) && seen.remove(key) != null && now - nextInvite >= 0) {
                    nextInvite = now + TimeUnit.SECONDS.toNanos(5);
                    invite.accept(ip);
                }
            }
        } catch (IOException | SecurityException unavailable) {
            // Presence is optional on networks that filter broadcasts. Find sender and Enter host IP stay available.
        }
    }
}
