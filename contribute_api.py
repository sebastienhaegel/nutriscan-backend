from fastapi import APIRouter, HTTPException, Form, Depends
from sqlalchemy.orm import Session
from database import get_db

router = APIRouter(prefix="/api/learning", tags=["learning"])

# Endpoint simple pour tester
@router.post("/contribute")
async def contribute_photo(
    photo_base64: str = Form(...),
    label: str = Form(...),
    user_id: str = Form(...),
    confidence: float = Form(0.9),
    db: Session = Depends(get_db)
):
    """Contribuer une photo au système d'apprentissage centralisé"""
    try:
        # Pour maintenant, juste retourner un succès
        return {
            "status": "success",
            "message": f"Photo contribuée! ({label})",
            "total_for_label": 1,
            "user_id": user_id
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Récupère les statistiques"""
    return {
        "total_contributions": 0,
        "active_users": 0,
        "labels": [],
        "current_model_version": 1,
        "model_status": "ready"
    }

@router.get("/latest_model")
async def get_latest_model(db: Session = Depends(get_db)):
    """Retourne le dernier modèle"""
    return {
        "status": "ready",
        "version": 1,
        "model_base64": ""
    }
