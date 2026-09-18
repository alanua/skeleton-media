package com.skeleton.home;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.hardware.ConsumerIrManager;
import org.json.JSONArray;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

public final class IrReceiver extends BroadcastReceiver {
    private static final Set<String> ALLOWED = new HashSet<>(Arrays.asList(
        "2c442bfdf4476ff6e98005ef54c8cfc6282a4d30a9aa45a84d57ad14265b60ee",
        "f54e743fc458f053e6c050a92bc9124f47a1445e30f7528dafdd125d1a24ac94",
        "1cad6befee3e1da8000097a0fa506ddbcb9a623f6968cbdd7b5daca17d929d2f",
        "ed54a54d4eee85a3bf462e7cc3be0c52b936cd3c7a6ae8afe302a1708f54ae75",
        "295cb5bf17eaabe698beecb62014ff2c2eba132584968b903620ca6feabc3008",
        "24fe7bc7119c03af4826f55406969113fee3b125c0edbb62b13046f7f40d8069",
        "fe2401122fdef9514e739deb315c61e8fa7758932f3dfdf05c423378614a6001",
        "c795b54d2bdf611833e16fbd0ef060e0d8e9a159f48eb7437d6a1fa1fdf15e11",
        "1ddec611a2058b8b61030b68a72a1dde14cb58d0e30dd9108e808e03613f3002"
    ));

    @Override public void onReceive(Context context, Intent intent) {
        if (intent == null || !"com.skeleton.home.IR_TRANSMIT".equals(intent.getAction())) return;
        int frequency = intent.getIntExtra("frequency", 0);
        String patternJson = intent.getStringExtra("pattern_json");
        String candidateId = intent.getStringExtra("candidate_id");
        if (frequency < 30000 || frequency > 60000 || patternJson == null || candidateId == null) return;
        try {
            JSONArray values = new JSONArray(patternJson);
            if (values.length() < 2 || values.length() > 512) return;
            int[] pattern = new int[values.length()];
            long total = 0L;
            StringBuilder canonical = new StringBuilder().append(frequency).append(':');
            for (int i = 0; i < values.length(); i++) {
                int duration = values.getInt(i);
                if (duration < 1 || duration > 100000) return;
                total += duration;
                if (total >= 1900000L) return;
                if (i > 0) canonical.append(',');
                canonical.append(duration);
                pattern[i] = duration;
            }
            String digest = sha256(canonical.toString());
            if (!ALLOWED.contains(digest) || !candidateId.equals("sharp-" + digest.substring(0, 16))) return;
            ConsumerIrManager ir = (ConsumerIrManager) context.getSystemService(Context.CONSUMER_IR_SERVICE);
            if (ir == null || !ir.hasIrEmitter()) return;
            ir.transmit(frequency, pattern);
            setResultCode(1);
            setResultData(candidateId);
        } catch (Exception ignored) { }
    }

    private static String sha256(String value) throws Exception {
        byte[] hash = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder out = new StringBuilder();
        for (byte b : hash) out.append(String.format("%02x", b & 0xff));
        return out.toString();
    }
}
