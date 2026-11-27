/* Runs the real Fundi Finder Python code (Pyodide, self-hosted) off the main thread.
   Messages in:  {id, type: "boot", saved}  |  {id, type: "call", name, args}  |  {id, type: "export"}  |  {id, type: "reset"}
   Messages out: {id, ok, data}  |  {type: "progress", text}                                          */

importScripts("../vendor/pyodide/pyodide.js");

let engine = null;

function progress(text) {
  self.postMessage({ type: "progress", text });
}

async function boot(saved) {
  progress("Loading the Python runtime (10 MB).");
  const pyodide = await loadPyodide({
    indexURL: new URL("../vendor/pyodide/", self.location.href).href,
  });
  progress("Loading SQLAlchemy and SQLite.");
  await pyodide.loadPackage(["sqlalchemy"]);
  progress("Unpacking Fundi Finder's source.");
  const zip = await (await fetch(new URL("../python/fundifinder-demo.zip", self.location.href))).arrayBuffer();
  pyodide.unpackArchive(zip, "zip", { extractDir: "/app" });
  pyodide.runPython("import sys; sys.path.insert(0, '/app')");
  engine = pyodide.pyimport("demo_engine");
  progress("Creating the in-memory database and the invented data.");
  const started = JSON.parse(engine.init(saved || null, null));
  const versions = JSON.parse(
    pyodide.runPython(
      "import json, sys, sqlalchemy; json.dumps({'python': sys.version.split()[0], 'sqlalchemy': sqlalchemy.__version__})"
    )
  );
  return { ...started, ...versions, pyodide: pyodide.version };
}

self.onmessage = async (event) => {
  const { id, type, name, args, saved } = event.data;
  try {
    let data;
    if (type === "boot") data = await boot(saved);
    else if (type === "call") data = JSON.parse(engine.call(name, JSON.stringify(args || {})));
    else if (type === "export") data = engine.export_state();
    else if (type === "reset") data = JSON.parse(engine.reset());
    else throw new Error("Unknown message type: " + type);
    self.postMessage({ id, ok: true, data });
  } catch (error) {
    self.postMessage({ id, ok: false, error: String((error && error.message) || error) });
  }
};
