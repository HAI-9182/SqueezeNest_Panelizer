"""Command Line Interface for SqueezeNest."""
import typer
from pathlib import Path
from typing import Optional
import sys

app = typer.Typer(
    name="squeezenest",
    help="General-purpose 2D irregular nesting and dimensional sensitivity engine."
)


def _print_report_errors(report: "ValidationReport") -> None:  # type: ignore[name-defined]
    """Print all ERROR-severity violations to stderr."""
    from squeezenest.api.models import ValidationReport  # noqa: PLC0415
    for err in report.errors():
        typer.echo(f" - [{err.code.value}] {err.message}", err=True)

@app.command()
def nest(
    dxf: Path = typer.Argument(..., help="Path to the input DXF file"),
    width: float = typer.Option(..., "--width", "-w", help="Stock sheet width (mm)"),
    height: float = typer.Option(..., "--height", "-h", help="Stock sheet height (mm)"),
    clearance: float = typer.Option(0.5, "--clearance", "-c", help="Clearance between parts (mm)"),
    strategy: str = typer.Option("blf", "--strategy", "-s", help="Nesting strategy (blf, lattice, ga)")
):
    """Run a basic nesting job."""
    typer.echo(f"Running nesting job for {dxf} on sheet {width}x{height} with strategy {strategy}")
    from squeezenest._io.dxf_ingest import ingest_dxf
    from squeezenest.api.models import NestingJob, StockSheet, PartMetadata, RotationSet, NestingStrategy

    if not dxf.exists():
        typer.echo(f"Error: File {dxf} not found.", err=True)
        raise typer.Exit(code=1)
        
    parts, report = ingest_dxf(dxf, eps_snap_mm=0.01)
    if report.is_fatal:
        typer.echo("Fatal errors during DXF ingestion.", err=True)
        _print_report_errors(report)
        raise typer.Exit(code=1)
        
    typer.echo(f"Successfully ingested {len(parts)} parts.")
    
    # Map ingested parts to PartMetadata
    job_parts = {}
    for i, p in enumerate(parts):
        part_id = f"PART_{i}"
        job_parts[part_id] = (p, PartMetadata(part_id=part_id, quantity=1))
        
    try:
        strat_enum = NestingStrategy(strategy.lower())
    except ValueError:
        typer.echo(f"Invalid strategy: {strategy}", err=True)
        raise typer.Exit(code=1)

    job = NestingJob(
        parts=job_parts,
        stock=[StockSheet(width_mm=width, height_mm=height)],
        clearance_mm=clearance,
        rotation_set=RotationSet.ORTHO,
        strategy=strat_enum
    )
    
    result = job.run()
    typer.echo(f"Nesting complete! Yield: {result.layout.yield_count} parts.")
    typer.echo(f"Utilisation: {result.layout.utilisation * 100:.2f}%")


@app.command()
def sweep(
    dxf: Path = typer.Argument(..., help="Path to the input DXF file"),
    width: float = typer.Option(..., "--width", "-w", help="Stock sheet width (mm)"),
    height: float = typer.Option(..., "--height", "-h", help="Stock sheet height (mm)")
):
    """Run a dimensional sensitivity sweep ('squeeze-to-fit')."""
    typer.echo("Running sensitivity sweep... (not fully hooked up to CLI args yet)")
    from squeezenest._io.dxf_ingest import ingest_dxf
    from squeezenest.api.models import NestingJob, StockSheet, PartMetadata, RotationSet, SensitivityConfig
    from squeezenest._sensitivity.sweep import run_sensitivity_sweep

    parts, report = ingest_dxf(dxf, eps_snap_mm=0.01)
    if report.is_fatal:
        typer.echo("Fatal errors during DXF ingestion.", err=True)
        _print_report_errors(report)
        raise typer.Exit(code=1)
        
    job_parts = {f"P{i}": (p, PartMetadata(part_id=f"P{i}")) for i, p in enumerate(parts)}
    job = NestingJob(
        parts=job_parts,
        stock=[StockSheet(width_mm=width, height_mm=height)],
        clearance_mm=0.5,
        rotation_set=RotationSet.ORTHO
    )
    config = SensitivityConfig()
    result = run_sensitivity_sweep(job, config)
    best = result.best_yield()
    typer.echo(f"Max Yield achieved: {best.yield_count} (scale_x={best.scale_x:.2f}, scale_y={best.scale_y:.2f})")


@app.command()
def serve(
    port: int = typer.Option(8080, "--port", "-p", help="Port to run the demo server on")
):
    """Launch the interactive web demo server."""
    typer.echo(f"Starting web demo on port {port}...")
    import subprocess
    import sys
    
    demo_script = Path(__file__).parent.parent / "demo" / "server.py"
    if not demo_script.exists():
        typer.echo(f"Demo script not found at {demo_script}", err=True)
        raise typer.Exit(code=1)
        
    try:
        subprocess.run([sys.executable, str(demo_script), "--port", str(port)])
    except KeyboardInterrupt:
        typer.echo("Server stopped.")

if __name__ == "__main__":
    app()
