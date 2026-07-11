function go(n){
  document.querySelectorAll(".screen")
    .forEach(s => s.classList.remove("active"));

  document.getElementById("s"+n).classList.add("active");
}

function selectLang(el){
  document.querySelectorAll(".lang")
    .forEach(l => l.classList.remove("active"));

  el.classList.add("active");
}

async function selectFolder(){
  const folder = await eel.select_folder()();
  document.getElementById("projectPath").innerText = folder;
}

async function startPipeline(){
  go(4);

  const status = document.getElementById("status");
  const fixed = document.getElementById("fixedCode");

  status.innerText = "Генерация патчей...";

  // 🔥 sci-fi эффект "потока"
  let dots = 0;
  const anim = setInterval(() => {
    dots = (dots + 1) % 4;
    status.innerText = "Генерация патчей" + ".".repeat(dots);
  }, 400);

  const result = await eel.run_pipeline()();

  clearInterval(anim);

  status.innerText = "Готово ✔";

  // ✨ подсветка "исправленного кода"
  fixed.innerText = highlight(result.fixed || "// no data");
}

function highlight(code){
  return code
    .replace(/fix/g, "✨fix✨")
    .replace(/error/g, "❌error❌");
}