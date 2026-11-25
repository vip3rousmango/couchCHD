package io.github.vip3rousmango.couchchd.receiver;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.os.Environment;
import android.os.storage.StorageManager;
import android.os.storage.StorageVolume;
import android.provider.DocumentsContract;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.WindowManager;
import android.widget.*;
import java.io.IOException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class ReceiverActivity extends Activity {
    private static final int PICK = 27, GRANTS = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION;
    // Even if an activity is destroyed during blocked I/O, a replacement cannot start a second writer.
    private static final ExecutorService IO = Executors.newSingleThreadExecutor();
    private Wire peer; private Wire.Manifest offer; private Uri tree;
    private SharedPreferences prefs; private LinearLayout panel; private TextView status;
    private String message = "Open Send over Wi-Fi on your computer. Connect this handheld to the same network.";
    private Job job; private boolean finished, destroyed;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state); prefs = getSharedPreferences("folders", MODE_PRIVATE);
        message += CardStore.interrupted(this);
        ScrollView scroll = new ScrollView(this); scroll.setFillViewport(true);
        panel = new LinearLayout(this); panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(20),dp(12),dp(20),dp(12)); scroll.addView(panel); setContentView(scroll);
        draw();
    }
    private int dp(int x) { return Math.round(x * getResources().getDisplayMetrics().density); }
    private TextView text(String label, int sp) {
        TextView v = new TextView(this); v.setText(label); v.setTextSize(sp);
        v.setPadding(0,0,0,dp(6)); panel.addView(v); return v;
    }
    private void button(String label, Runnable press) {
        Button b = new Button(this); b.setText(label); b.setAllCaps(false); b.setMinHeight(dp(48));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1,-2); p.topMargin = dp(4);
        b.setOnClickListener(v -> press.run()); panel.addView(b,p);
    }
    private void draw() {
        if (destroyed) return;
        panel.removeAllViews(); String title = finished ? "Transfer complete" : offer != null ? label() + " files" : peer != null ? "Compare the pairing code" : "Receive over Wi-Fi";
        text(title,24).setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        if (peer != null && offer == null) text(peer.code,28).setTypeface(Typeface.MONOSPACE,Typeface.BOLD);
        if (offer != null && !finished) {
            text(offer.entries.size() + (offer.entries.size()==1 ? " file" : " files") + " · " + android.text.format.Formatter.formatFileSize(this,offer.bytes),18);
            if (tree != null) text("Destination: " + DocumentsContract.getTreeDocumentId(tree),16);
            if (tree != null) text("Keep Receive open. Wait for Transfer complete before opening or syncing Daijishō.",16);
        }
        status = text(message,17);
        if (job != null) { button("Stop", this::stop); return; }
        if (finished) { button("Find another sender", this::reset); return; }
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
    private final class Job {
        boolean canceled; Thread thread;
        synchronized void start() { thread = Thread.currentThread(); if (canceled) thread.interrupt(); }
        synchronized void cancel() { canceled = true; if (thread != null) thread.interrupt(); }
        synchronized void ended() { thread = null; Thread.interrupted(); }
        void progress(String s) { runOnUiThread(() -> { if (!destroyed && job == this && !canceled) { message=s; status.setText(s); } }); }
    }
    private void run(String start, Work work) {
        if (job != null) return;
        Job j = new Job(); job = j; message = start; getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); draw();
        IO.execute(() -> {
            j.start(); Runnable result;
            try { Wire.check(); result = work.run(j); }
            catch (Exception e) {
                String why = e.getMessage(); result = () -> message = (j.canceled ? "Stopped. " : "Not completed. ") +
                    (why == null ? e.getClass().getSimpleName() : why) + CardStore.interrupted(this);
            } finally { j.ended(); }
            Runnable finalResult = result;
            runOnUiThread(() -> { if (destroyed || job != j) return;
                job = null; getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); finalResult.run(); draw(); });
        });
    }
    private void find(String address) {
        run("Looking for the sender…", j -> {
            Wire connected = new Wire(Wire.find(address)); connected.pair();
            return () -> { peer = connected; if (!address.isEmpty()) prefs.edit().putString("host",address).apply();
                message="Sender: " + peer.peer.host + ". Approve on the computer only if its code matches. Then confirm here."; };
        });
    }
    private void approved() {
        Wire session = peer;
        run("Waiting for approval on the computer…", j -> {
            long until = System.nanoTime() + 120000000000L;
            while (System.nanoTime() < until) {
                Wire.Manifest manifest = session.manifest();
                if (manifest != null) return () -> {
                    offer = manifest; String saved = prefs.getString("folder:" + offer.system,null);
                    tree = saved == null ? null : Uri.parse(saved);
                    message = "Use the existing " + label() + " folder indexed by your frontend, not the card root.";
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
            message = "Ready. Existing files are checked, never replaced.";
        } catch (Exception invalid) { message = "Folder not selected. " + invalid.getMessage(); }
        draw();
    }
    private void receive() {
        Wire session = peer; Wire.Manifest selection = offer; Uri folder = tree;
        run("Checking your platform folder…", j -> {
            CardStore store = new CardStore(getApplicationContext(), folder);
            int copied = store.receive(session,selection,j::progress);
            j.progress("All files verified. Closing the sender…"); session.done();
            return () -> { finished = true; message = copied + " added · " + (selection.entries.size()-copied) + " already identical. Refresh this platform in your frontend."; };
        });
    }
    private void stop() { if (job != null) { job.cancel(); message="Stopping safely…"; status.setText(message); } }
    private void reset() { peer = null; offer = null; tree = null; finished = false; message="Open a new Send over Wi-Fi session on your computer."; draw(); }
    @Override protected void onStop() { super.onStop(); if (!isChangingConfigurations()) stop(); }
    @Override protected void onDestroy() { destroyed = true; stop(); super.onDestroy(); }
    @Override public boolean onKeyUp(int key, KeyEvent e) {
        // Android gamepads may send BUTTON_A rather than DPAD_CENTER / ENTER.
        if (key == KeyEvent.KEYCODE_BUTTON_A && getCurrentFocus() instanceof Button) return getCurrentFocus().performClick();
        if (key == KeyEvent.KEYCODE_BUTTON_B) { onBackPressed(); return true; }
        return super.onKeyUp(key,e);
    }
}
