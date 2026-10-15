# PlatformIO extra script: adds "Analyze latest run" under PROJECT TASKS > Custom in the
# PlatformIO sidebar. It only runs tools/analyze_run.py - it never builds or uploads.
import os
import shutil

Import("env")  # noqa: F821  (provided by PlatformIO)


def system_python():
    # PlatformIO's own Python has no matplotlib, so prefer the one on PATH (it draws the plot)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if ".platformio" in d.lower():
            continue
        exe = shutil.which("python", path=d)
        if exe:
            return exe
    return env.subst("$PYTHONEXE")  # noqa: F821  fallback: still works, just no plot


py = system_python()
script = os.path.join("$PROJECT_DIR", "tools", "analyze_run.py")

env.AddCustomTarget(  # noqa: F821
    name="analyze",
    dependencies=None,
    actions='"%s" "%s"' % (py, script),
    title="Analyze latest run",
    description="Average the steady plateaus of the newest log (tools/analyze_run.py)",
    always_build=True,
)
