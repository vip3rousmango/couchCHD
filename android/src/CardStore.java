package io.github.vip3rousmango.couchchd.receiver;

import android.content.ContentResolver;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsContract;
import android.provider.DocumentsContract.Document;
import android.system.Os;
import android.system.OsConstants;
import android.system.StructStat;
import android.webkit.MimeTypeMap;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** Accept only the system's local ExternalStorageProvider, whose createDocument uses
 * mkdir/createNewFile, not truncate or replace. A SAF rename is NOT a no-clobber operation. */
final class CardStore {
    private static final String AUTH = "com.android.externalstorage.documents";
    private static final String[] COLUMNS = {Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE};
    private final ContentResolver resolver; private final Uri tree, root;
    private final SharedPreferences journal;
    private final Map<Uri,Map<String,Node>> cache = new HashMap<>();
    private static final class Node {
        final Uri uri; final String name; final boolean dir;
        Node(Uri u,String n,String mime) { uri=u; name=n; dir=Document.MIME_TYPE_DIR.equals(mime); }
    }
    static void checkTree(Uri tree) throws IOException {
        if (!"content".equals(tree.getScheme()) || !AUTH.equals(tree.getAuthority()) || !DocumentsContract.isTreeUri(tree))
            throw new IOException("Choose the SD card or internal storage in the Android Files picker, not a cloud provider.");
        String id = DocumentsContract.getTreeDocumentId(tree); int colon = id.indexOf(':');
        if (colon < 1 || colon == id.length()-1) throw new IOException("Choose a platform folder, not the entire volume.");
        String path = id.substring(colon+1);
        for (String p : path.split("/",-1)) if (p.isEmpty() || p.equals(".") || p.equals("..")) throw new IOException("Invalid folder.");
        if (path.equalsIgnoreCase("Android") || path.toLowerCase(Locale.ROOT).startsWith("android/")) throw new IOException("Choose a game folder outside Android app data.");
    }
    CardStore(Context context, Uri tree) throws IOException {
        checkTree(tree); this.tree=tree; resolver=context.getContentResolver();
        root=doc(DocumentsContract.getTreeDocumentId(tree)); journal=context.getSharedPreferences("receive-journal",Context.MODE_PRIVATE);
        if (!info(root).dir) throw new IOException("Platform folder is missing. Choose the folder again.");
    }
    private Uri doc(String id) { return DocumentsContract.buildDocumentUriUsingTree(tree,id); }
    private Node node(Cursor c) throws IOException {
        if (c.isNull(0) || c.isNull(1) || c.isNull(2)) throw new IOException("Incomplete storage directory listing.");
        return new Node(doc(c.getString(0)),c.getString(1),c.getString(2));
    }
    private Node info(Uri uri) throws IOException {
        try (Cursor c = resolver.query(uri,COLUMNS,null,null,null)) {
            if (c == null || !c.moveToFirst()) throw new IOException("Cannot inspect the selected card document.");
            Node n=node(c); if (!n.uri.equals(uri)) throw new IOException("Storage returned a different document."); return n;
        }
    }
    private Node lookup(Uri parent,String name,boolean fresh) throws IOException {
        Map<String,Node> children = cache.get(parent);
        if (fresh || children == null) {
            Uri uri=DocumentsContract.buildChildDocumentsUriUsingTree(tree,DocumentsContract.getDocumentId(parent));
            children=new HashMap<>();
            try (Cursor c = resolver.query(uri,COLUMNS,null,null,null)) {
                if (c == null || c.getExtras().getBoolean(DocumentsContract.EXTRA_LOADING) || c.getExtras().containsKey(DocumentsContract.EXTRA_ERROR))
                    throw new IOException("Cannot read the complete platform folder.");
                while (c.moveToNext()) {
                    Wire.check(); Node n=node(c); String key=Rules.fold(n.name);
                    if (children.containsKey(key)) children.put(key,null); else children.put(key,n);
                }
            }
            cache.put(parent,children);
        }
        String key=Rules.fold(name); Node n=children.get(key);
        if (children.containsKey(key) && (n == null || !n.name.equals(name))) throw new IOException("Ambiguous or differently spelled name on card: " + name);
        return n;
    }
    private Uri parent(Wire.Entry e, boolean create) throws Exception {
        Uri parent=root;
        for (int i=0; i<e.parts.length-1; i++) {
            String name=e.parts[i]; Node n=lookup(parent,name,false);
            if (n==null && create) n=lookup(parent,name,true);
            if (n==null) {
                if (!create) return null;
                Uri uri=DocumentsContract.createDocument(resolver,parent,Document.MIME_TYPE_DIR,name);
                if (uri==null) throw new IOException("Could not create folder: " + name);
                n=info(uri); cache.remove(parent);
                if (!n.name.equals(name)) throw new IOException("Storage changed the folder name to " + n.name + ". Inspect the empty folder before retrying.");
            }
            if (!n.dir) throw new IOException("A file occupies the folder name " + name);
            parent=n.uri;
        }
        return parent;
    }
    private ParcelFileDescriptor open(Uri uri,String mode) throws IOException {
        ParcelFileDescriptor p=resolver.openFileDescriptor(uri,mode);
        if (p==null) throw new IOException("Card document is unavailable."); return p;
    }
    private String key(Wire.Entry e) { return tree.toString()+"\n"+e.path; }
    private void pending(Wire.Entry e,String evidence) throws IOException {
        if (!journal.edit().putString(key(e),evidence).commit()) throw new IOException("Cannot save private transfer journal.");
    }
    private void clear(Wire.Entry e) throws IOException {
        if (journal.contains(key(e)) && !journal.edit().remove(key(e)).commit()) throw new IOException("Cannot clear private transfer journal.");
    }
    static String interrupted(Context context) {
        Map<String,?> all=context.getSharedPreferences("receive-journal",Context.MODE_PRIVATE).getAll();
        if (all.isEmpty()) return "";
        String key=all.keySet().iterator().next();
        return " Previous receive was interrupted: " + key.substring(key.indexOf('\n')+1) + ". Retry the same selection. Inspect any differing partial file yourself; nothing is removed automatically after restart.";
    }
    private void validatePlaylist(byte[] bytes, Wire.Entry e, Wire.Manifest offer) throws Exception {
        String content=StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString();
        if (content.startsWith("\ufeff")) content=content.substring(1);
        String prefix=e.path.substring(0,e.path.lastIndexOf('/')+1); int count=0;
        for (String line : content.split("\\r\\n|\\n|\\r")) {
            if (line.trim().isEmpty() || line.startsWith("#")) continue;
            String member=line.startsWith("./") ? line.substring(2) : line; Rules.parts(member);
            if (!member.toLowerCase(Locale.ROOT).endsWith(".chd") || !offer.paths.contains(prefix+member))
                throw new IOException("Playlist refers outside this transfer: " + e.path);
            count++;
        }
        if (count==0) throw new IOException("Empty playlist: " + e.path);
    }
    private static final class Verified {
        final Uri uri; final StructStat stamp;
        Verified(Uri uri, StructStat stamp) { this.uri=uri; this.stamp=stamp; }
        void requireSame(StructStat now, String path) throws IOException {
            if (stamp.st_dev!=now.st_dev || stamp.st_ino!=now.st_ino || stamp.st_size!=now.st_size ||
                    stamp.st_mtime!=now.st_mtime || stamp.st_ctime!=now.st_ctime)
                throw new IOException("Card file changed during transfer: " + path);
        }
    }
    private final Map<String,Verified> verifiedFiles=new HashMap<>();
    private void verify(Uri uri, Wire.Entry e, Wire.Manifest offer, Wire.Progress progress) throws Exception {
        boolean playlist=Rules.playlist(e.path);
        ByteArrayOutputStream list=playlist ? new ByteArrayOutputStream((int)e.size) : null;
        try (ParcelFileDescriptor fd=open(uri,"r");
             InputStream in=new ParcelFileDescriptor.AutoCloseInputStream(ParcelFileDescriptor.dup(fd.getFileDescriptor()))) {
            Verified v=new Verified(uri,Os.fstat(fd.getFileDescriptor()));
            e.verify(in,list,progress); v.requireSame(Os.fstat(fd.getFileDescriptor()),e.path); verifiedFiles.put(e.path,v);
        }
        if (list!=null) validatePlaylist(list.toByteArray(),e,offer);
    }
    private void same(Node n, Wire.Entry e, Wire.Manifest offer, Wire.Progress progress) throws Exception {
        if (n.dir) throw new IOException("A folder occupies " + e.path);
        try { verify(n.uri,e,offer,progress); }
        catch (InterruptedIOException stop) { throw stop; }
        catch (Exception different) {
            throw new IOException("Existing file was not changed: " + e.path + ". " +
                (journal.contains(key(e)) ? "An interrupted transfer was recorded. Inspect it and move it aside only if you recognize it as partial." : "Check that this is the right folder, or move the conflict yourself."),different);
        }
        clear(e);
    }
    // The dup keeps the created inode alive even if its path is replaced. Never delete by
    // name/URI alone; after process death there is no live ownership proof, only guidance.
    private boolean removeOwned(Uri uri, ParcelFileDescriptor guard, String actualName) {
        if (guard==null || actualName==null) return false;
        try (ParcelFileDescriptor seen=open(uri,"r")) {
            StructStat held=Os.fstat(guard.getFileDescriptor()), current=Os.fstat(seen.getFileDescriptor());
            if (held.st_ino<=0 || held.st_nlink!=1 || !OsConstants.S_ISREG(held.st_mode) || held.st_ino!=current.st_ino || held.st_dev!=current.st_dev || !info(uri).name.equals(actualName)) return false;
            return DocumentsContract.deleteDocument(resolver,uri);
        } catch (Exception uncertain) { return false; }
    }
    private void add(Wire session, Wire.Entry e, Wire.Manifest offer, Uri parent, String name, Wire.Progress progress) throws Exception {
        // Playlists are small: authenticate and validate every reference before creating their entry point.
        byte[] list=null;
        if (Rules.playlist(name)) {
            ByteArrayOutputStream bytes=new ByteArrayOutputStream((int)e.size); session.fetch(e,bytes,null);
            list=bytes.toByteArray(); validatePlaylist(list,e,offer);
        }
        Wire.check(); pending(e,"Creating new document; expected SHA-256 " + Rules.hex(e.digest));
        Uri created=null; String actual=null; ParcelFileDescriptor guard=null; boolean verified=false;
        try {
            String mime=MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substring(name.lastIndexOf('.')+1).toLowerCase(Locale.ROOT));
            created=DocumentsContract.createDocument(resolver,parent,mime==null ? "application/octet-stream" : mime,name); cache.remove(parent);
            if (created==null) throw new IOException("Could not create " + e.path);
            Node made=info(created); actual=made.name;
            if (made.dir || !AUTH.equals(created.getAuthority())) throw new IOException("Storage did not create a local file.");
            // 'rw', never 'w' or 'rwt': opening must not truncate anything, even on error.
            try (ParcelFileDescriptor rw=open(created,"rw")) {
                StructStat st=Os.fstat(rw.getFileDescriptor());
                if (st.st_size!=0 || !OsConstants.S_ISREG(st.st_mode) || st.st_ino<=0 || st.st_nlink!=1)
                    throw new IOException("Storage did not return a newly created empty file.");
                guard=ParcelFileDescriptor.dup(rw.getFileDescriptor());
                if (!actual.equals(name)) throw new IOException("Name already taken or changed by storage: " + name + " → " + actual);
                pending(e,created + "\n" + st.st_dev + ":" + st.st_ino + "\n" + Rules.hex(e.digest));
                Wire.check();
                try (FileOutputStream out=new ParcelFileDescriptor.AutoCloseOutputStream(ParcelFileDescriptor.dup(rw.getFileDescriptor()))) {
                    if (list==null) session.fetch(e,out,progress); else out.write(list);
                    out.flush(); out.getFD().sync();
                }
            }
            progress.show("Checking the card copy: " + e.path); verify(created,e,offer,progress);
            verified=true; clear(e);
        } catch (Exception failure) {
            if (!verified) {
                if (created!=null && !removeOwned(created,guard,actual))
                    throw new IOException("Not completed. Inspect " + (actual==null?e.path:actual) + " on the card before retrying. " + failure.getMessage(),failure);
                if (created!=null) clear(e);
            }
            throw failure;
        } finally { if (guard!=null) guard.close(); }
    }
    int receive(Wire session, Wire.Manifest offer, Wire.Progress progress) throws Exception {
        int added=0, index=0;
        for (Wire.Entry e : offer.entries) {
            Wire.check(); Uri parent=parent(e,false);
            if (parent==null) continue;
            Node n=lookup(parent,e.parts[e.parts.length-1],false);
            if (n!=null) { progress.show("Checking existing file: " + e.path); same(n,e,offer,progress); }
        }
        for (Wire.Entry e : offer.entries) {
            Wire.check(); index++; if (verifiedFiles.containsKey(e.path)) continue;
            progress.show(index + " of " + offer.entries.size() + " · " + e.path);
            Uri parent=parent(e,true); String name=e.parts[e.parts.length-1]; Node n=lookup(parent,name,true);
            if (n!=null) { same(n,e,offer,progress); continue; }
            add(session,e,offer,parent,name,progress); added++;
        }
        // A card removed, folder renamed or file replaced since preflight is not a completed import.
        progress.show("Checking the complete selection…");
        for (Wire.Entry e : offer.entries) {
            Wire.check(); Verified v=verifiedFiles.get(e.path);
            if (v==null) throw new IOException("Missing verification for " + e.path);
            try (ParcelFileDescriptor fd=open(v.uri,"r")) { v.requireSame(Os.fstat(fd.getFileDescriptor()),e.path); }
        }
        return added;
    }
}
