package io.github.vip3rousmango.couchchd.receiver;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.content.res.ColorStateList;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.graphics.drawable.StateListDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Build;
import android.os.Environment;
import android.os.storage.StorageManager;
import android.os.storage.StorageVolume;
import android.provider.DocumentsContract;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.KeyEvent;
import android.view.WindowManager;
import android.widget.*;
import java.io.IOException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.Locale;

public final class ReceiverActivity extends Activity {
    private static final int PICK = 27, GRANTS = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION;
    // Even if an activity is destroyed during blocked I/O, a replacement cannot start a second writer.
    private static final ExecutorService IO = Executors.newSingleThreadExecutor();
    private Wire peer; private Wire.Manifest offer; private Uri tree;
    private static final int CANVAS = 0xff101d1c, INK = 0xffedf7f4, MUTED = 0xffb1c7c1, MINT = 0xff84dfc4;
    private static final String FIRST_STEP = "On the same Wi-Fi, open Send over Wi-Fi in CouchCHD on your computer. Then find it here.";
    private final String model = Rules.deviceLabel(Build.MODEL);
    private SharedPreferences prefs;
    private LinearLayout panel, actions; private ScrollView scroll;
    private TextView heading, status, file, amounts;
    private ProgressBar meter;
    private int senderIcon, handheldIcon;
    private String message = FIRST_STEP, failureTitle;
    private Job job; private boolean finished, destroyed;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state); prefs = getSharedPreferences("folders", MODE_PRIVATE);
        message += CardStore.interrupted(this);
        getWindow().setStatusBarColor(CANVAS); getWindow().setNavigationBarColor(CANVAS);
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(CANVAS);
        scroll = new ScrollView(this); scroll.setFillViewport(true);
        panel = new LinearLayout(this); panel.setOrientation(LinearLayout.VERTICAL); panel.setPadding(dp(16),dp(10),dp(16),dp(6));
        scroll.addView(panel); root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        actions = new LinearLayout(this); actions.setPadding(dp(16),dp(4),dp(16),dp(8));
        root.addView(actions,new LinearLayout.LayoutParams(-1,-2)); setContentView(root);
        // This SDK-only APK is linked by aapt2 without generating a Java R class.
        senderIcon = getResources().getIdentifier("sender","drawable",getPackageName());
        handheldIcon = getApplicationInfo().icon;
        draw();
    }
    private int dp(int x) { return Math.round(x * getResources().getDisplayMetrics().density); }
    private TextView text(LinearLayout parent, String label, int sp, int color) {
        TextView v = new TextView(this); v.setText(label); v.setTextSize(sp); v.setTextColor(color);
        v.setPadding(0,0,0,dp(3)); parent.addView(v); return v;
    }
    private GradientDrawable shape(int fill, int line, int width) {
        GradientDrawable d = new GradientDrawable(); d.setColor(fill); d.setCornerRadius(dp(8));
        if (width > 0) d.setStroke(dp(width),line); return d;
    }
    private void button(String label, Runnable press) {
        boolean primary = actions.getChildCount() == 0;
        Button b = new Button(this); b.setText(label); b.setAllCaps(false); b.setMinHeight(dp(48));
        b.setMinWidth(0); b.setPadding(dp(5),dp(4),dp(5),dp(4));
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{android.R.attr.state_focused},shape(0xffe0fff4,0xffffc86f,3));
        states.addState(new int[]{},shape(primary ? MINT : 0xff263b37,0,0));
        b.setBackgroundTintList(null);
        b.setBackground(new RippleDrawable(ColorStateList.valueOf(0x55788884),states,null));
        b.setTextColor(new ColorStateList(new int[][]{{android.R.attr.state_focused},{}},new int[]{CANVAS,primary ? CANVAS : INK}));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0,-2,1);
        if (!primary) p.leftMargin = dp(8);
        b.setOnClickListener(v -> press.run()); actions.addView(b,p);
        if (primary && !b.isInTouchMode()) b.requestFocus();
    }
    private void device(LinearLayout strip, int icon, String role, String name, String address) {
        LinearLayout unit = new LinearLayout(this); unit.setGravity(Gravity.CENTER_VERTICAL);
        strip.addView(unit,new LinearLayout.LayoutParams(0,-2,1));
        ImageView picture = new ImageView(this); picture.setImageResource(icon);
        picture.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        unit.addView(picture,new LinearLayout.LayoutParams(dp(38),dp(38)));
        LinearLayout names = new LinearLayout(this); names.setOrientation(LinearLayout.VERTICAL); names.setPadding(dp(7),0,dp(5),0);
        unit.addView(names,new LinearLayout.LayoutParams(0,-2,1));
        text(names,role,12,MUTED).setPadding(0,0,0,0);
        TextView n = text(names,name,16,INK); n.setTypeface(Typeface.DEFAULT,Typeface.BOLD);
        n.setMaxLines(2); n.setEllipsize(TextUtils.TruncateAt.END);
        if (!address.isEmpty()) text(names,address,13,MUTED);
    }
    private void devices() {
        LinearLayout strip = new LinearLayout(this); strip.setGravity(Gravity.CENTER_VERTICAL);
        strip.setPadding(0,dp(6),0,dp(6)); panel.addView(strip);
        String host = peer == null ? "" : peer.hostLabel;
        device(strip,senderIcon,"Sender",peer == null ? "Not paired" : host.isEmpty() ? peer.peer.host : host,
               peer == null || host.isEmpty() ? "" : peer.peer.host);
        device(strip,handheldIcon,"This handheld",model.isEmpty() ? "Android handheld" : model,"");
    }
    private void draw() {
        if (destroyed) return;
        panel.removeAllViews(); actions.removeAllViews(); scroll.scrollTo(0,0);
        String title = job != null ? job.title : failureTitle != null ? failureTitle : finished ? "Transfer complete" :
            offer != null ? (tree == null ? "Choose a destination" : "Ready to receive") :
            peer != null ? "Compare the pairing code" : "Receive over Wi-Fi";
        heading = text(panel,title,24,INK); heading.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        if (peer != null && offer == null) text(panel,peer.code,26,MINT).setTypeface(Typeface.MONOSPACE,Typeface.BOLD);
        if (job != null && offer != null) {
            file = text(panel,"",17,INK); file.setMaxLines(2); file.setEllipsize(TextUtils.TruncateAt.END);
            meter = new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal); meter.setMax(1000);
            meter.setProgressTintList(ColorStateList.valueOf(MINT)); meter.setVisibility(View.GONE);
            panel.addView(meter,new LinearLayout.LayoutParams(-1,dp(9)));
            amounts = text(panel,"",15,MUTED); amounts.setTypeface(Typeface.MONOSPACE);
        }
        devices();
        if (offer != null && !finished && job == null) {
            text(panel,label() + " · " + offer.entries.size() + (offer.entries.size()==1 ? " file" : " files") +
                 " · " + android.text.format.Formatter.formatFileSize(this,offer.bytes),17,INK);
            if (tree != null) text(panel,"Folder: " + DocumentsContract.getTreeDocumentId(tree),15,MUTED);
        }
        status = text(panel,message,16,MUTED);
        if (job != null) { button("Stop", this::stop); return; }
        if (finished) { button("New transfer", this::reset); return; }
        if (peer == null) {
            button("Find sender", () -> find("")); button("Enter host IP", this::manual);
        } else if (offer == null) {
            button("Codes match", this::approved); button("Disconnect", this::reset);
        } else {
            if (tree != null) button("Receive files", this::receive);
            button(tree == null ? "Choose folder" : "Change folder", this::pick); button("Disconnect", this::reset);
        }
    }
    private String label() {
        try { return Rules.systemLabel(offer.system); } catch (IOException impossible) { throw new AssertionError(impossible); }
    }
    private void manual() {
        EditText ip = new EditText(this); ip.setHint("192.168.1.20"); ip.setSingleLine(true);
        ip.setInputType(InputType.TYPE_CLASS_PHONE); ip.setText(prefs.getString("host", "")); ip.selectAll();
        new AlertDialog.Builder(this).setTitle("Sender IPv4 address").setView(ip).setNegativeButton("Cancel",null)
            .setPositiveButton("Find sender",(d,w)-> {
                String address = ip.getText().toString().trim(); if (!address.isEmpty()) find(address);
            }).show();
    }
    private interface Work { Runnable run(Job j) throws Exception; }
    private static String amount(long bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024L * 1024) return String.format(Locale.ROOT,"%.1f KiB",bytes / 1024.0);
        if (bytes < 1024L * 1024 * 1024) return String.format(Locale.ROOT,"%.1f MiB",bytes / (1024.0 * 1024));
        return String.format(Locale.ROOT,"%.2f GiB",bytes / (1024.0 * 1024 * 1024));
    }
    private final class Job implements Wire.Progress {
        final String title;
        final Handler ui = new Handler(Looper.getMainLooper());
        boolean canceled, queued; Thread thread;
        Wire.Phase phase; Wire.Entry entry; long bytes; double speed;
        Job(String title) { this.title = title; }
        synchronized void start() { thread = Thread.currentThread(); if (canceled) thread.interrupt(); }
        synchronized void cancel() { canceled = true; if (thread != null) thread.interrupt(); }
        synchronized void ended() { thread = null; Thread.interrupted(); }
        // A folder of tiny already-owned files must not queue one UI event for every hash.
        public synchronized void show(Wire.Phase p, Wire.Entry e, long n, double rate) {
            if (canceled) return;
            phase = p; entry = e; bytes = n; speed = rate;
            if (!queued) { queued = true; ui.postDelayed(this::render,80); }
        }
        private synchronized void render() {
            queued = false; if (destroyed || job != this || canceled) return;
            heading.setText(phase.title);
            meter.setVisibility(entry == null ? View.GONE : View.VISIBLE);
            if (entry == null) { file.setText(""); amounts.setText(""); return; }
            file.setText(entry.position + " / " + offer.entries.size() + "   " + entry.path);
            meter.setProgress((int)(1000.0 * bytes / entry.size));
            String detail = amount(bytes) + " / " + amount(entry.size);
            if (phase == Wire.Phase.COPY) detail += Double.isNaN(speed) ? " · Waiting for data" : " · " + amount(Math.round(speed)) + "/s";
            amounts.setText(detail);
            ui.removeCallbacks(staleRate);
            if (phase == Wire.Phase.COPY && bytes < entry.size && !Double.isNaN(speed)) ui.postDelayed(staleRate,2000);
        }
        private final Runnable staleRate = () -> {
            synchronized (this) {
                if (!destroyed && job == this && !canceled && !queued && phase == Wire.Phase.COPY)
                    amounts.setText(amount(bytes) + " / " + amount(entry.size) + " · No recent bytes");
            }
        };
    }
    private void run(String title, String hint, Work work) {
        if (job != null) return;
        Job j = new Job(title); job = j; message = hint; failureTitle = null;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); draw();
        IO.execute(() -> {
            j.start(); Runnable result;
            try { Wire.check(); result = work.run(j); }
            catch (Exception e) {
                result = () -> {
                    String why = e.getMessage();
                    boolean lost = !j.canceled && Wire.connectionLost(e);
                    failureTitle = j.canceled ? "Stopped" : lost ? "Disconnected" : peer == null ? "No sender paired" :
                        offer == null ? "Pairing not complete" : "Transfer not complete";
                    if (lost) { peer = null; offer = null; tree = null; }
                    message = (j.canceled ? "Operation stopped. " : "") +
                        (why == null ? e.getClass().getSimpleName() : why) +
                        (lost ? " Start a new sender session and find it here." : "") + CardStore.interrupted(this);
                };
            } finally { j.ended(); }
            Runnable finalResult = result;
            runOnUiThread(() -> { if (destroyed || job != j) return;
                job = null; j.ui.removeCallbacksAndMessages(null);
                getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); finalResult.run(); draw(); });
        });
    }
    private void find(String address) {
        run("Looking for sender","Searching this Wi-Fi network. No files are being copied.", j -> {
            Wire connected = new Wire(Wire.find(address)); connected.pair(model); Wire.check();
            return () -> { peer = connected; if (!address.isEmpty()) prefs.edit().putString("host",address).apply();
                message="Only approve on the computer if both codes match. Then confirm here."; };
        });
    }
    private void approved() {
        Wire session = peer;
        run("Waiting for approval","Approve this code on the sender. No files are being copied.", j -> {
            long until = System.nanoTime() + 120000000000L;
            while (System.nanoTime() < until) {
                Wire.Manifest manifest = session.manifest();
                Wire.check();
                if (manifest != null) return () -> {
                    offer = manifest; String saved = prefs.getString("folder:" + offer.system,null);
                    tree = saved == null ? null : Uri.parse(saved);
                    message = tree == null ? "Sender approved. Choose the existing " + label() + " folder used by Daijishō or your frontend, not the card root." :
                        "Sender approved. Check your saved folder, then receive. Existing files are never replaced.";
                };
                Thread.sleep(1000);
            }
            throw new IOException("No host approval yet. Check the computer, then confirm again.");
        });
    }
    private void pick() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        if (tree != null) i.putExtra(DocumentsContract.EXTRA_INITIAL_URI,tree);
        else if (Build.VERSION.SDK_INT >= 29) {
            StorageManager storage = getSystemService(StorageManager.class);
            StorageVolume initial = storage.getPrimaryStorageVolume();
            for (StorageVolume volume : storage.getStorageVolumes()) {
                if (volume.isRemovable() && Environment.MEDIA_MOUNTED.equals(volume.getState())) { initial=volume; break; }
            }
            i = initial.createOpenDocumentTreeIntent();
        }
        i.addFlags(GRANTS | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
        startActivityForResult(i,PICK);
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request,result,data);
        if (request != PICK || result != RESULT_OK || data == null || data.getData() == null || offer == null) return;
        Uri next = data.getData();
        try {
            CardStore.checkTree(next);
            int flags = data.getFlags() & GRANTS; if (flags != GRANTS) throw new IOException("The folder needs both read and write access.");
            getContentResolver().takePersistableUriPermission(next,flags);
            Uri old = tree; tree = next; prefs.edit().putString("folder:" + offer.system,tree.toString()).apply();
            if (old != null && !old.equals(next) && !prefs.getAll().containsValue(old.toString())) {
                try { getContentResolver().releasePersistableUriPermission(old,GRANTS); } catch (SecurityException ignored) { }
            }
            failureTitle = null; message = "Sender approved. Existing files are checked, never replaced.";
        } catch (Exception invalid) { message = "Folder not selected. " + invalid.getMessage(); }
        draw();
    }
    private void receive() {
        Wire session = peer; Wire.Manifest selection = offer; Uri folder = tree;
        run("Checking destination","Keep Receive open until Transfer complete.", j -> {
            CardStore store = new CardStore(getApplicationContext(), folder);
            int copied = store.receive(session,selection,j);
            j.show(Wire.Phase.CLOSE,null,0,Double.NaN); boolean acknowledged = session.done();
            return () -> { finished = true; message = copied + " added · " + (selection.entries.size()-copied) + " already identical. " +
                (acknowledged ? "Session closed. " : "Files verified; sender closure not confirmed. ") +
                "Refresh " + label() + " in Daijishō or your frontend."; };
        });
    }
    private void stop() {
        if (job != null) { job.cancel(); heading.setText("Stopping safely"); status.setText("Waiting for the current operation to stop.");
            if (offer != null) amounts.setText(""); }
    }
    private void reset() {
        failureTitle = finished ? null : "Disconnected";
        peer = null; offer = null; tree = null; finished = false;
        message="Start a new Send over Wi-Fi session on your computer, then find it here."; draw();
    }
    @Override protected void onStop() { super.onStop(); if (!isChangingConfigurations()) stop(); }
    @Override protected void onDestroy() {
        destroyed = true; stop(); if (job != null) job.ui.removeCallbacksAndMessages(null); super.onDestroy();
    }
    @Override public void onBackPressed() {
        if (job != null) stop();
        else if (peer != null) reset();
        else super.onBackPressed();
    }
    @Override public boolean onKeyUp(int key, KeyEvent e) {
        // Android gamepads may send BUTTON_A rather than DPAD_CENTER / ENTER.
        if (key == KeyEvent.KEYCODE_BUTTON_A && getCurrentFocus() instanceof Button) return getCurrentFocus().performClick();
        if (key == KeyEvent.KEYCODE_BUTTON_B) { onBackPressed(); return true; }
        return super.onKeyUp(key,e);
    }
}
