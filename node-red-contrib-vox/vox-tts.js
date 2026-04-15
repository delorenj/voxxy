// node-red-contrib-vox: synthesize speech via the vox service.
// Accepts msg.payload (text) and msg.voice (optional override), returns a
// Buffer in msg.payload with WAV bytes suitable for exec/file-out/http response.

module.exports = function (RED) {
    function VoxTtsNode(config) {
        RED.nodes.createNode(this, config);
        const node = this;
        node.url = config.url || "http://vox:8000";
        node.voice = config.voice || "";
        node.cfg = parseFloat(config.cfg || "2.0");
        node.steps = parseInt(config.steps || "10", 10);

        node.on("input", async function (msg, send, done) {
            send = send || function () { node.send.apply(node, arguments); };
            const text = (typeof msg.payload === "string" ? msg.payload : "").trim();
            if (!text) {
                node.status({ fill: "red", shape: "ring", text: "no text" });
                if (done) done(new Error("msg.payload must be a non-empty string"));
                return;
            }

            const voice = msg.voice || node.voice || null;
            const body = {
                text: text,
                voice: voice || undefined,
                cfg: msg.cfg || node.cfg,
                steps: msg.steps || node.steps,
            };

            node.status({ fill: "blue", shape: "dot", text: "synthesizing" });
            try {
                const res = await fetch(`${node.url}/synthesize`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
                if (!res.ok) {
                    const detail = await res.text();
                    throw new Error(`vox ${res.status}: ${detail}`);
                }
                const arrayBuf = await res.arrayBuffer();
                msg.payload = Buffer.from(arrayBuf);
                msg.contentType = "audio/wav";
                msg.voice = voice;
                node.status({ fill: "green", shape: "dot", text: `${msg.payload.length} bytes` });
                send(msg);
                if (done) done();
            } catch (err) {
                node.status({ fill: "red", shape: "ring", text: err.message.slice(0, 30) });
                if (done) done(err); else node.error(err, msg);
            }
        });
    }
    RED.nodes.registerType("vox-tts", VoxTtsNode);
};
