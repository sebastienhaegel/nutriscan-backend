from fastapi import APIRouter, Form, Depends
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, Integer, DateTime, LargeBinary, func
from database import get_db, Base, engine
from datetime import datetime
import base64

router = APIRouter(prefix="/api/learning", tags=["learning"])

# Modèle de base de données
class ContributionModel(Base):
    __tablename__ = "contributions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    label = Column(String, index=True)
    confidence = Column(Integer)
    photo_data = Column(LargeBinary)
    created_at = Column(DateTime, default=datetime.utcnow)

# Créer les tables
Base.metadata.create_all(bind=engine)

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
        confidence_pct = int(confidence * 100)
        
        try:
            photo_bytes = base64.b64decode(photo_base64)
        except:
            photo_bytes = b""
        
        contribution = ContributionModel(
            user_id=user_id,
            label=label,
            confidence=confidence_pct,
            photo_data=photo_bytes
        )
        
        db.add(contribution)
        db.commit()
        db.refresh(contribution)
        
        total_for_label = db.query(func.count(ContributionModel.id)).filter(
            ContributionModel.label == label
        ).scalar()
        
        retraining_triggered = (total_for_label % 20 == 0) and (total_for_label > 0)
        
        return {
            "status": "success",
            "message": f"Photo contribuée! ({label})",
            "total_for_label": total_for_label,
            "user_id": user_id,
            "retraining_triggered": retraining_triggered
        }
        
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": str(e)}

@router.get("/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Récupère les statistiques"""
    try:
        total = db.query(func.count(ContributionModel.id)).scalar() or 0
        unique_users = db.query(func.count(func.distinct(ContributionModel.user_id))).scalar() or 0
        
        labels_counts = db.query(
            ContributionModel.label,
            func.count(ContributionModel.id).label("count")
        ).group_by(ContributionModel.label).all()
        
        labels = [{"label": label, "count": count} for label, count in labels_counts]
        
        return {
            "status": "success",
            "total_contributions": total,
            "active_users": unique_users,
            "labels": labels,
            "current_model_version": 1,
            "model_status": "ready"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/latest_model")
async def get_latest_model(db: Session = Depends(get_db)):
    """Retourne le dernier modèle"""
    return {
        "status": "ready",
        "version": 1,
        "model_base64": "",
        "created_at": datetime.utcnow().isoformat()
    }

@router.get("/user/{user_id}/history")
async def get_user_history(user_id: str, db: Session = Depends(get_db)):
    """Historique des contributions"""
    try:
        contributions = db.query(ContributionModel).filter(
            ContributionModel.user_id == user_id
        ).order_by(ContributionModel.created_at.desc()).limit(50).all()
        
        return {
            "status": "success",
            "user_id": user_id,
            "total": len(contributions),
            "contributions": [
                {
                    "label": c.label,
                    "confidence": c.confidence,
                    "timestamp": c.created_at.isoformat()
                }
                for c in contributions
            ]
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
