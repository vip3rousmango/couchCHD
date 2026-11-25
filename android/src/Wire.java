package io.github.vip3rousmango.couchchd.receiver;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.*;
import javax.net.ssl.*;

final class Wire {
    enum Phase {
        COPY("Receiving over Wi-Fi"), EXISTING("Checking existing file"), READBACK("Verifying card copy"),
        SELECTION("Verifying selection"), CLOSE("Closing sender session");
        final String title;
        Phase(String title) { this.title = title; }
    }
    interface Progress { void show(Phase phase, Entry entry, long bytes, double bytesPerSecond); }
    static void check() throws InterruptedIOException {
        if (Thread.currentThread().isInterrupted()) throw new InterruptedIOException("Stopped.");
    }
    static final class Peer {
        final String host; final int port; final byte[] pin;
        Peer(String host, int port, byte[] pin) { this.host = host; this.port = port; this.pin = pin; }
    }
    private static InetAddress ipv4(String host) throws IOException {
        String[] fields = host.split("\\.",-1); byte[] b = new byte[4];
        if (fields.length != 4) throw new IOException("Enter the sender’s IPv4 address, without a port.");
        for (int i = 0; i < 4; i++) {
            if (!fields[i].matches("0|[1-9][0-9]{0,2}") || Integer.parseInt(fields[i]) > 255)
                throw new IOException("Enter a valid IPv4 address.");
            b[i] = (byte)Integer.parseInt(fields[i]);
        }
        return InetAddress.getByAddress(b);
    }
    static List<Peer> find(String host) throws Exception {
        Set<InetAddress> targets = new LinkedHashSet<>();
        InetAddress manual = host.isEmpty() ? null : ipv4(host);
        if (manual != null) targets.add(manual);
        else {
            targets.add(ipv4("255.255.255.255"));
            for (NetworkInterface iface : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!iface.isUp() || iface.isLoopback()) continue;
                for (InterfaceAddress a : iface.getInterfaceAddresses())
                    if (a.getBroadcast() != null) targets.add(a.getBroadcast());
            }
        }
        byte[] request = "CouchCHD/1 find".getBytes(StandardCharsets.US_ASCII), buf = new byte[1024];
        // Keep the whole broadcast response window: the Deck and the PC may both be listening.
        Map<String,Peer> found = new TreeMap<>();
        try (DatagramSocket sock = new DatagramSocket()) {
            sock.setBroadcast(true); sock.setSoTimeout(300);
            for (int round = 0; round < 4; round++) {
                for (InetAddress dst : targets) {
                    try { sock.send(new DatagramPacket(request, request.length, dst, 42846)); }
                    catch (IOException e) { if (manual != null) throw e; }
                }
                long until = System.nanoTime() + 900000000L;
                while (System.nanoTime() < until) {
                    check(); DatagramPacket p = new DatagramPacket(buf, buf.length);
                    try { sock.receive(p); } catch (SocketTimeoutException timeout) { continue; }
                    if (p.getPort() != 42846 || !(p.getAddress() instanceof Inet4Address) ||
                        (manual != null && !manual.equals(p.getAddress()))) continue;
                    Peer peer;
                    try {
                        JSONObject j = new JSONObject(new String(buf, 0, p.getLength(), StandardCharsets.UTF_8));
                        int port = j.getInt("port"); byte[] pin = Rules.digestBytes(j.getString("fingerprint"));
                        if (port <= 0 || port > 65535) continue;
                        peer = new Peer(p.getAddress().getHostAddress(), port, pin);
                    } catch (JSONException | IOException invalid) { continue; }
                    if (manual != null) return Collections.singletonList(peer);
                    Peer previous = found.put(peer.host, peer);
                    if (previous != null && (previous.port != peer.port || !MessageDigest.isEqual(previous.pin, peer.pin)))
                        throw new IOException("Sender changed during discovery. Find it again and compare the code.");
                    if (found.size() > 32) throw new IOException("Too many senders. Enter the host IP instead.");
                }
            }
        }
        if (!found.isEmpty()) return new ArrayList<>(found.values());
        throw new IOException("No sender found. Open Connect a handheld on the same network, or enter its IPv4 address.");
    }
    final Peer peer; final String code;
    String hostLabel = "", hostKind = "";
    private final String token; private final SSLContext tls;
    Wire(Peer peer) throws Exception {
        this.peer = peer;
        byte[] secret = new byte[32]; new SecureRandom().nextBytes(secret);
        token = Rules.hex(secret); code = Rules.code(peer.pin, secret);
        tls = SSLContext.getInstance("TLS");
        tls.init(null, new TrustManager[]{new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] chain, String type) throws CertificateException { throw new CertificateException(); }
            public void checkServerTrusted(X509Certificate[] chain, String type) throws CertificateException {
                if (chain == null || chain.length == 0 || !MessageDigest.isEqual(Rules.sha().digest(chain[0].getEncoded()), peer.pin))
                    throw new CertificateException("Sender certificate changed. Start pairing again.");
            }
        }}, null);
    }
    private HttpsURLConnection connection(String path, boolean post) throws IOException {
        check(); URL url = new URL("https", peer.host, peer.port, path);
        HttpsURLConnection c = (HttpsURLConnection)url.openConnection(Proxy.NO_PROXY);
        c.setSSLSocketFactory(tls.getSocketFactory());
        c.setHostnameVerifier((host, session) -> {
            try { return host.equals(peer.host) && MessageDigest.isEqual(Rules.sha().digest(session.getPeerCertificates()[0].getEncoded()), peer.pin); }
            catch (Exception badCert) { return false; }
        });
        c.setInstanceFollowRedirects(false); c.setUseCaches(false);
        c.setConnectTimeout(7000); c.setReadTimeout(15000);
        c.setRequestProperty("Authorization", "Bearer " + token);
        c.setRequestProperty("Connection", "close"); c.setRequestProperty("Accept-Encoding", "identity");
        if (post) { c.setRequestMethod("POST"); c.setDoOutput(true); c.setFixedLengthStreamingMode(0); }
        return c;
    }
    private static String json(HttpsURLConnection c, int limit) throws IOException {
        if (c.getContentLengthLong() > limit) throw new IOException("Sender response too large.");
        try (InputStream in = c.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192]; int n;
            while ((n = in.read(buffer)) != -1) {
                check(); if (out.size() + n > limit) throw new IOException("Sender response too large.");
                out.write(buffer,0,n);
            }
            return out.toString("UTF-8");
        }
    }
    static final class Unavailable extends IOException {
        Unavailable(String message) { super(message); }
        Unavailable(IOException cause) { super(cause.getMessage(),cause); }
    }
    static boolean connectionLost(Throwable problem) {
        for (Throwable e = problem; e != null; e = e.getCause()) {
            if (e instanceof Unavailable) return true;
        }
        return false;
    }
    private static void ok(HttpsURLConnection c) throws IOException {
        int rc = c.getResponseCode();
        if (rc != 200) throw new Unavailable("Sender returned " + rc + ". Start a new connection on the host.");
    }
    void pair(String model) throws Exception {
        HttpsURLConnection c = connection("/pair", true);
        try {
            if (!model.isEmpty()) c.setRequestProperty("X-CouchCHD-Device", model);
            c.getOutputStream().close(); ok(c);
            JSONObject reply = new JSONObject(json(c, 4096));
            if (!code.equals(reply.getString("code"))) throw new IOException("Pairing codes disagree. Close the sender.");
            Object host = reply.opt("host");
            if (host instanceof String) hostLabel = Rules.deviceLabel((String)host);
            String kind = reply.optString("kind");
            if ("deck".equals(kind) || "pi".equals(kind)) hostKind = kind;  // Display hint, not identity.
        } catch (SocketException | SocketTimeoutException | SSLException | EOFException e) {
            throw new Unavailable(e);
        } finally { c.disconnect(); }
    }
    interface Waiting { void show(boolean preparing); }
    Manifest awaitManifest(Waiting waiting) throws Exception {
        long approvalDeadline = System.nanoTime() + 120000000000L;
        String lastState = "";
        while (true) {
            HttpsURLConnection c = connection("/manifest", false);
            try {
                c.setRequestProperty("X-CouchCHD-Wait", "1");
                int rc = c.getResponseCode();
                if (rc == 200) return new Manifest(new JSONObject(json(c, 4 * 1024 * 1024)));
                if (rc == 202) {
                    String state = new JSONObject(json(c, 4096)).getString("state");
                    if (!state.equals("waiting") && !state.equals("preparing"))
                        throw new Unavailable("Unknown sender state. Update CouchCHD on both devices.");
                    if (!lastState.equals(state)) waiting.show(state.equals("preparing"));
                    lastState = state;
                } else if (rc == 403 && lastState.isEmpty()) {
                    if (System.nanoTime() >= approvalDeadline)
                        throw new IOException("No host approval yet. Check the computer, then confirm again.");
                } else throw new Unavailable("Sender returned " + rc + ". Start a new connection on the host.");
            } catch (SocketException | SocketTimeoutException | SSLException | EOFException e) {
                throw new Unavailable(e);
            } finally { c.disconnect(); }
            check(); Thread.sleep(1000);
        }
    }
    void fetch(Entry e, OutputStream out, Progress progress) throws Exception {
        HttpsURLConnection c = connection("/file/" + e.id, false);
        try {
            progress.show(Phase.COPY,e,0,Double.NaN);
            InputStream payload;
            try {
                ok(c); long length = c.getContentLengthLong();
                if (length >= 0 && length != e.size) throw new IOException("File size changed: " + e.path);
                payload = c.getInputStream();
            } catch (IOException unavailable) { throw new Unavailable(unavailable); }
            try (InputStream in = payload) { e.verify(in, out, Phase.COPY, progress); }
        } finally { c.disconnect(); }
    }
    boolean done() { return finish("/done"); }
    void disconnect() { finish("/disconnect"); }
    private boolean finish(String path) {
        try { HttpsURLConnection c = connection(path, true);
            // A disconnected receiver must not stall a subsequent Find behind long transfer timeouts.
            if ("/disconnect".equals(path)) { c.setConnectTimeout(1500); c.setReadTimeout(1500); }
            try { c.getOutputStream().close(); ok(c); return true; } finally { c.disconnect(); }
        } catch (IOException ignored) { return false; } // Best effort; never treat this as a card write failure.
    }
    private static long integer(JSONObject j, String key) throws JSONException, IOException {
        Object value = j.get(key);
        if (!(value instanceof Integer) && !(value instanceof Long)) throw new IOException("Expected an integer for " + key);
        return ((Number)value).longValue();
    }
    static final class Entry {
        final int id; final long size; final String path; final String[] parts; final byte[] digest;
        Entry(JSONObject j) throws Exception {
            long value = integer(j,"id"); size = integer(j,"size"); path = j.getString("path");
            parts = Rules.parts(path); digest = Rules.digestBytes(j.getString("sha256"));
            if (value < 0 || value > Integer.MAX_VALUE || size < 1 || (Rules.playlist(path) && size > 1024*1024))
                throw new IOException("Invalid entry: " + path);
            id = (int)value;
        }
        int position;
        void verify(InputStream in, OutputStream copy, Phase phase, Progress progress) throws IOException {
            if (in == null) throw new IOException("Cannot read " + path);
            long total = 0, sampleTime = System.nanoTime(), sampleBytes = 0; int n;
            if (phase != Phase.COPY) progress.show(phase,this,0,Double.NaN);
            MessageDigest hash = Rules.sha(); byte[] b = new byte[128*1024];
            while (true) {
                try { n = in.read(b); }
                catch (IOException ex) { if (phase == Phase.COPY) throw new Unavailable(ex); throw ex; }
                if (n < 0) break;
                check(); if (size-total < n) throw new IOException("Size differs: " + path);
                hash.update(b,0,n); if (copy != null) copy.write(b,0,n); total += n;
                long now = System.nanoTime();
                if (now - sampleTime >= 400000000L || total == size) {
                    // COPY is used only with the pinned HTTPS response stream, never a SAF read.
                    double rate = phase == Phase.COPY && now > sampleTime
                        ? (total - sampleBytes) * 1e9 / (now - sampleTime) : Double.NaN;
                    progress.show(phase,this,total,rate); sampleTime = now; sampleBytes = total;
                }
            }
            if (phase == Phase.COPY && total < size) throw new Unavailable("Sender stopped before " + path + " was complete.");
            if (total != size || !MessageDigest.isEqual(hash.digest(), digest)) throw new IOException("Contents differ: " + path);
        }
    }
    static final class Manifest {
        final String system; final List<Entry> entries = new ArrayList<>(); final long bytes;
        final Set<String> paths = new HashSet<>();
        Manifest(JSONObject j) throws Exception {
            system = j.getString("system"); Rules.systemLabel(system);
            JSONArray a = j.getJSONArray("files"); if (a.length() < 1 || a.length() > 10000) throw new IOException("Invalid file count.");
            Set<Integer> ids = new HashSet<>(); Set<String> folded = new HashSet<>(); long sum = 0;
            for (int i = 0; i < a.length(); i++) {
                Entry e = new Entry(a.getJSONObject(i));
                if (!ids.add(e.id) || !folded.add(Rules.fold(e.path)) || !Rules.supported(system, e.path) || sum > Long.MAX_VALUE-e.size)
                    throw new IOException("Conflicting or unsupported manifest entry: " + e.path);
                paths.add(e.path); entries.add(e); sum += e.size;
            }
            entries.sort(Comparator.comparing(e -> Rules.playlist(e.path))); bytes = sum;
            for (int i = 0; i < entries.size(); i++) entries.get(i).position = i + 1;
        }
    }
}
