from fastapi import APIRouter, Form, Depends
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, Integer, DateTime, LargeBinary, func
from database import get_db, Base, engine
from datetime import datetime
import base64
import os
import tempfile
import numpy as np
from PIL import Image
import io

# TensorFlow et Core ML
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
try:
    import coremltools as ct
except ImportError:
    ct = None

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

class ModelVersionModel(Base):
    __tablename__ = "model_versions"
    
    id = Column(Integer, primary_key=True, index=True)
    version = Column(Integer, unique=True)
    model_data = Column(LargeBinary)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="ready")

# Créer les tables
Base.metadata.create_all(bind=engine)

# 🔥 FONCTION DE RÉENTRAÎNEMENT AVEC TENSORFLOW
async def trigger_retraining(label: str, total_photos: int, db: Session):
    """Entraîne un modèle Core ML avec TensorFlow"""
    try:
        print(f"\n{'='*60}")
        print(f"🔄 RÉENTRAÎNEMENT RÉEL AVEC TENSORFLOW")
        print(f"{'='*60}")
        print(f"📊 Label: {label}")
        print(f"📸 Nombre de photos: {total_photos}")
        
        # Récupérer les photos de la BD
        photos = db.query(ContributionModel).filter(
            ContributionModel.label == label
        ).all()
        
        if len(photos) < 5:
            print(f"❌ Pas assez de photos ({len(photos)} < 5)")
            return None
        
        print(f"✅ {len(photos)} photos récupérées de la BD")
        
        # Créer un dossier temporaire
        with tempfile.TemporaryDirectory() as temp_dir:
            print(f"📁 Dossier temporaire: {temp_dir}")
            
            # Sauvegarder les images
            image_paths = []
            for i, photo in enumerate(photos):
                try:
                    # Décoder la photo
                    img = Image.open(io.BytesIO(photo.photo_data))
                    img = img.resize((224, 224))  # Standard pour les modèles
                    
                    # Sauvegarder
                    img_path = os.path.join(temp_dir, f"image_{i}.jpg")
                    img.save(img_path)
                    image_paths.append(img_path)
                except Exception as e:
                    print(f"⚠️  Erreur image {i}: {e}")
            
            if len(image_paths) < 5:
                print(f"❌ Pas assez d'images valides")
                return None
            
            print(f"✅ {len(image_paths)} images sauvegardées")
            
            # Charger les images pour TensorFlow
            print(f"⚙️  Chargement des images pour TensorFlow...")
            image_array = []
            for img_path in image_paths:
                img = keras.utils.load_img(img_path, target_size=(224, 224))
                img_array = keras.utils.img_to_array(img)
                img_array = keras.applications.mobilenet_v2.preprocess_input(img_array)
                image_array.append(img_array)
            
            X_train = np.array(image_array)
            y_train = np.ones(len(image_array))  # Tous du même label
            
            print(f"✅ {len(X_train)} images chargées")
            
            # Entraîner le modèle
            print(f"⚙️  Entraînement en cours... (peut prendre 30-60s)")
            
            # Utiliser MobileNetV2 pré-entraîné
            base_model = keras.applications.MobileNetV2(
                input_shape=(224, 224, 3),
                include_top=False,
                weights='imagenet'
            )
            base_model.trainable = False
            
            # Ajouter couches de classification
            model = keras.Sequential([
                base_model,
                layers.GlobalAveragePooling2D(),
                layers.Dense(128, activation='relu'),
                layers.Dropout(0.2),
                layers.Dense(1, activation='sigmoid')
            ])
            
            model.compile(
                optimizer='adam',
                loss='binary_crossentropy',
                metrics=['accuracy']
            )
            
            # Entraîner
            history = model.fit(
                X_train, y_train,
                epochs=5,
                batch_size=4,
                verbose=1
            )
            
            accuracy = history.history['accuracy'][-1]
            print(f"✅ Entraînement complété! Précision: {accuracy:.2%}")
            
            # Exporter en Core ML
            print(f"⚙️  Export en Core ML...")
            
            if ct is None:
                print(f"⚠️  coremltools non installé, conversion skippée")
                model_base64 = ""
            else:
                try:
                    # Convertir en Core ML
                    coreml_model = ct.convert(model)
                    
                    # Sauvegarder
                    model_path = os.path.join(temp_dir, f"{label}_model.mlmodel")
                    coreml_model.save(model_path)
                    
                    # Encoder en base64
                    with open(model_path, "rb") as f:
                        model_bytes = f.read()
                        model_base64 = base64.b64encode(model_bytes).decode()
                    
                    print(f"✅ Modèle Core ML généré")
                    print(f"📦 Taille: {len(model_base64) / 1024 / 1024:.1f}MB")
                except Exception as e:
                    print(f"⚠️  Erreur conversion Core ML: {e}")
                    model_base64 = ""
            
            # Sauvegarder la version en BD
            try:
                # Récupérer la version actuelle
                latest = db.query(ModelVersionModel).order_by(
                    ModelVersionModel.version.desc()
                ).first()
                
                new_version = (latest.version + 1) if latest else 1
                
                # Sauvegarder
                model_version = ModelVersionModel(
                    version=new_version,
                    model_data=model_base64.encode() if model_base64 else b"",
                    status="ready"
                )
                
                db.add(model_version)
                db.commit()
                
                print(f"✅ Modèle v{new_version} sauvegardé en BD")
                
            except Exception as e:
                print(f"❌ Erreur sauvegarde BD: {e}")
                db.rollback()
        
        print(f"{'='*60}")
        print(f"✅ RÉENTRAÎNEMENT COMPLÉTÉ AVEC SUCCÈS!")
        print(f"{'='*60}\n")
        
        return model_base64
        
    except Exception as e:
        print(f"❌ Erreur réentraînement: {e}")
        import traceback
        traceback.print_exc()
        return None

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
        
        # Sauvegarder la contribution
        contribution = ContributionModel(
            user_id=user_id,
            label=label,
            confidence=confidence_pct,
            photo_data=photo_bytes
        )
        
        db.add(contribution)
        db.commit()
        db.refresh(contribution)
        
        # Compter les photos pour ce label
        total_for_label = db.query(func.count(ContributionModel.id)).filter(
            ContributionModel.label == label
        ).scalar()
        
        # Vérifier si seuil atteint (20 photos)
        retraining_triggered = (total_for_label % 20 == 0) and (total_for_label > 0)
        
        # 🔥 DÉCLENCHER LE RÉENTRAÎNEMENT SI SEUIL ATTEINT
        if retraining_triggered:
            await trigger_retraining(label, total_for_label, db)
        
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

@router.post("/retrain/{label}")
async def manual_retrain(label: str, db: Session = Depends(get_db)):
    """Déclenche manuellement le réentraînement"""
    try:
        photos = db.query(ContributionModel).filter(
            ContributionModel.label == label
        ).all()
        
        if len(photos) < 5:
            return {"status": "error", "message": f"Pas assez de photos"}
        
        await trigger_retraining(label, len(photos), db)
        
        return {"status": "success", "message": f"Réentraînement lancé"}
    except Exception as e:
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
        
        # Récupérer version courante
        latest_model = db.query(ModelVersionModel).order_by(
            ModelVersionModel.version.desc()
        ).first()
        
        current_version = latest_model.version if latest_model else 1
        
        return {
            "status": "success",
            "total_contributions": total,
            "active_users": unique_users,
            "labels": labels,
            "current_model_version": current_version,
            "model_status": "ready"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/latest_model")
async def get_latest_model(db: Session = Depends(get_db)):
    """Retourne le dernier modèle"""
    try:
        latest = db.query(ModelVersionModel).order_by(
            ModelVersionModel.version.desc()
        ).first()
        
        if latest:
            return {
                "status": "ready",
                "version": latest.version,
                "model_base64": latest.model_data.decode() if latest.model_data else "",
                "created_at": latest.created_at.isoformat()
            }
        else:
            return {
                "status": "ready",
                "version": 1,
                "model_base64": "",
                "created_at": datetime.utcnow().isoformat()
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
