"""
Endpoints pour contribuer des données au système d'apprentissage centralisé
Railway Backend - NutriScan Centralized Learning
"""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import func
import uuid
import base64
from datetime import datetime
import asyncio
from typing import Optional
import logging

router = APIRouter(prefix="/api/learning", tags=["learning"])
logger = logging.getLogger(__name__)

# Modèle SQLAlchemy pour les contributions
from database import Base, get_db, engine
from sqlalchemy import Column, String, Float, DateTime, Integer, LargeBinary
import json

class ContributionData(Base):
    """
    Stocke chaque photo + label contribuée par un utilisateur
    """
    __tablename__ = "contributions"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)  # Tracker utilisateur
    label = Column(String, nullable=False, index=True)    # "ananas", "pomme", etc.
    photo_hash = Column(String, unique=True, nullable=False)
    photo_base64 = Column(LargeBinary, nullable=False)     # Photo complète en base64
    confidence = Column(Float, default=0.0)                # Confiance de Claude
    timestamp = Column(DateTime, default=datetime.utcnow)
    model_version = Column(String)                         # Version modèle qui a échoué
    
class ModelVersion(Base):
    """
    Historique des versions de modèles Core ML
    """
    __tablename__ = "model_versions"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    version = Column(Integer, nullable=False, unique=True, index=True)
    status = Column(String, default="ready")  # "training", "ready", "failed"
    created_at = Column(DateTime, default=datetime.utcnow)
    training_photos = Column(Integer, default=0)
    accuracy = Column(Float, nullable=True)
    error_message = Column(String, nullable=True)
    is_active = Column(Boolean, default=False)  # Version active actuellement
    
Base.metadata.create_all(bind=engine)


# ============================================
# 1. ENDPOINT : Contribuer une photo
# ============================================

@router.post("/contribute")
async def contribute_photo(
    photo_base64: str = Form(...),
    label: str = Form(...),
    user_id: str = Form(...),
    confidence: float = Form(0.9),
    db: Session = dependency(get_db)
):
    """
    Contribuer une photo et son label au système d'apprentissage centralisé
    
    Args:
        photo_base64: Photo encodée en base64
        label: Label du plat ("ananas", "pomme", etc.)
        user_id: ID unique de l'utilisateur (pour tracking)
        confidence: Confiance de Claude (0-1)
    
    Returns:
        Statut contribution + info si réentraînement déclenché
    """
    try:
        # Sécurité : valider label
        valid_labels = [
            "ananas", "pomme", "orange", "banane", "fraise",
            "pizza", "pates", "riz", "pain", "fromage",
            "poulet", "poisson", "boeuf", "salade", "soupe",
            "gateau", "glace", "chocolat", "yaourt", "lait"
        ]
        
        if label.lower() not in valid_labels:
            raise HTTPException(status_code=400, detail=f"Label '{label}' non reconnu")
        
        # Créer hash de la photo
        photo_hash = hash(photo_base64)
        
        # Vérifier si photo déjà dans DB
        existing = db.query(ContributionData).filter(
            ContributionData.photo_hash == photo_hash
        ).first()
        
        if existing:
            logger.warning(f"Photo dupliquée de {user_id} : {label}")
            return {
                "status": "duplicate",
                "message": "Cette photo a déjà été contribuée"
            }
        
        # Sauvegarder contribution
        contribution = ContributionData(
            user_id=user_id,
            label=label.lower(),
            photo_hash=photo_hash,
            photo_base64=photo_base64.encode('utf-8'),
            confidence=confidence,
            timestamp=datetime.utcnow()
        )
        
        db.add(contribution)
        db.commit()
        
        logger.info(f"✅ Photo contribuée: user={user_id}, label={label}")
        
        # Compter photos par label
        count = db.query(func.count(ContributionData.id)).filter(
            ContributionData.label == label.lower()
        ).scalar()
        
        response = {
            "status": "success",
            "message": f"Photo contribuée! ({count} total pour {label})",
            "total_for_label": count,
            "user_id": user_id
        }
        
        # ⚡ DÉCLENCHER RÉENTRAÎNEMENT si 50 photos atteintes
        if count % 50 == 0 and count > 0:
            logger.info(f"🔄 SEUIL 50 PHOTOS ATTEINT pour {label}! Réentraînement déclenché")
            
            # Lancer réentraînement en arrière-plan
            asyncio.create_task(trigger_retraining(label, db))
            
            response["retraining_triggered"] = True
            response["message"] = f"🎉 Réentraînement lancé! {count} photos pour {label}"
        
        return response
    
    except Exception as e:
        logger.error(f"❌ Erreur contribution: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 2. ENDPOINT : Récupérer stats apprentissage
# ============================================

@router.get("/stats")
async def get_stats(db: Session = dependency(get_db)):
    """
    Récupère statistiques du système d'apprentissage
    """
    # Total contributions
    total_contributions = db.query(func.count(ContributionData.id)).scalar()
    
    # Contributions par label
    label_stats = db.query(
        ContributionData.label,
        func.count(ContributionData.id).label("count")
    ).group_by(ContributionData.label).all()
    
    # Utilisateurs actifs
    active_users = db.query(func.count(func.distinct(ContributionData.user_id))).scalar()
    
    # Versions modèles
    latest_model = db.query(ModelVersion).filter(
        ModelVersion.is_active == True
    ).first()
    
    return {
        "total_contributions": total_contributions,
        "active_users": active_users,
        "labels": [{"label": l, "count": c} for l, c in label_stats],
        "current_model_version": latest_model.version if latest_model else 0,
        "model_status": latest_model.status if latest_model else "none"
    }


# ============================================
# 3. ENDPOINT : Télécharger dernier modèle
# ============================================

@router.get("/latest_model")
async def get_latest_model(db: Session = dependency(get_db)):
    """
    Retourne le dernier modèle Core ML prêt
    """
    latest = db.query(ModelVersion).filter(
        ModelVersion.status == "ready",
        ModelVersion.is_active == True
    ).order_by(ModelVersion.version.desc()).first()
    
    if not latest:
        return {
            "status": "no_model",
            "message": "Aucun modèle disponible pour le moment"
        }
    
    # Lire fichier modèle
    model_path = f"models/v{latest.version}/NutriScan.mlmodel"
    
    try:
        with open(model_path, "rb") as f:
            model_data = f.read()
        
        return {
            "version": latest.version,
            "status": "ready",
            "model_base64": base64.b64encode(model_data).decode(),
            "created_at": latest.created_at,
            "training_photos": latest.training_photos
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "message": "Fichier modèle introuvable"
        }


# ============================================
# 4. ENDPOINT : Historique contributions user
# ============================================

@router.get("/user/{user_id}/history")
async def get_user_history(user_id: str, db: Session = dependency(get_db)):
    """
    Récupère historique des contributions d'un utilisateur
    """
    contributions = db.query(ContributionData).filter(
        ContributionData.user_id == user_id
    ).order_by(ContributionData.timestamp.desc()).limit(50).all()
    
    return {
        "user_id": user_id,
        "total": len(contributions),
        "contributions": [
            {
                "label": c.label,
                "confidence": c.confidence,
                "timestamp": c.timestamp.isoformat()
            }
            for c in contributions
        ]
    }


# ============================================
# 5. FONCTION : Déclencher réentraînement
# ============================================

async def trigger_retraining(label: str, db: Session):
    """
    Lance le réentraînement du modèle Core ML
    Appelé en arrière-plan quand 50 photos atteintes
    """
    logger.info(f"🚀 Démarrage réentraînement pour '{label}'...")
    
    try:
        # Créer nouvelle version
        latest_version = db.query(ModelVersion).order_by(
            ModelVersion.version.desc()
        ).first()
        
        new_version_number = (latest_version.version + 1) if latest_version else 1
        
        new_model = ModelVersion(
            version=new_version_number,
            status="training",
            training_photos=0
        )
        
        db.add(new_model)
        db.commit()
        
        logger.info(f"📦 Version modèle créée: v{new_version_number}")
        
        # Importer la fonction de réentraînement
        from train_model import retrain_coreml_model
        
        # Lancer réentraînement
        success = await retrain_coreml_model(new_version_number, db)
        
        if success:
            # Marquer nouvelle version comme active
            new_model.status = "ready"
            new_model.is_active = True
            
            # Désactiver ancienne version
            old_models = db.query(ModelVersion).filter(
                ModelVersion.version < new_version_number,
                ModelVersion.is_active == True
            ).all()
            
            for old in old_models:
                old.is_active = False
            
            db.commit()
            
            logger.info(f"✅ Modèle v{new_version_number} PRÊT et activé!")
            
            # Notifier utilisateurs
            await notify_users_new_model(new_version_number)
        else:
            new_model.status = "failed"
            db.commit()
            logger.error(f"❌ Réentraînement v{new_version_number} ÉCHOUÉ")
    
    except Exception as e:
        logger.error(f"❌ Erreur réentraînement: {str(e)}")
        db.rollback()


# ============================================
# 6. FONCTION : Notifier utilisateurs
# ============================================

async def notify_users_new_model(version: int):
    """
    Envoie notification push à tous les utilisateurs
    """
    # TODO: Implémenter système de push notifications
    logger.info(f"📢 Notification: Nouveau modèle v{version} disponible!")
    pass
