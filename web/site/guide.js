// Point repository links at the repository named in pipeline.json.
"use strict";
fetch("/pipeline.json").then((r) => r.json()).then((p) => {
  const a = document.getElementById("remote-doc");
  if (a) a.href = p.repository + "/blob/main/docs/remote-setup.md";
}).catch(() => {});
