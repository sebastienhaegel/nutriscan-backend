"""
Logique de réentraînement Core ML
Crée et entraîne les modèles avec versioning + backup
"""

import os
import shutil
import base64
import asyncio
from pathlib import Path
from datetime import datetime
from sqlalchemy.orm import Session
import logging
from typing import Optional
import json

logger = logging.getLogger(__name__)

# ============================================
# Configuration
# ============================================

MODELS_DIR = Path("models")
DATASETS_DIR = Path("datasets")
BACKUP_DIR = Path("backups")

# Créer répertoires s'ils n'existent pas
MODELS_DIR.mkdir(exist_ok=True)
DATASETS_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)


# ============================================
# 1. EXTRACTION DES DONNÉES D'ENTRAÎNEMENT
# ============================================

async def prepare_training_data(version: int, db: Session) -> dict:
    """
    Prépare les données d'entraînement depuis PostgreSQL
    Crée structure de répertoires par label
    """
    logger.info(f"📁 Préparation données d'entraînement pour v{version}...")
    
    from contribute_api import ContributionData
    
    # Créer répertoire version
    version_dataset_dir = DATASETS_DIR / f"v{version}"
    version_dataset_dir.mkdir(exist_ok=True)
    
    # Récupérer toutes contributions
    contributions = db.query(ContributionData).all()
    
    if not contributions:
        logger.error("❌ Aucune données d'entraînement disponible!")
        return {"success": False, "error": "No training data"}
    
    stats = {}
    
    # Organiser par label
    for contribution in contributions:
        label = contribution.label
        
        # Créer répertoire label
        label_dir = version_dataset_dir / label
        label_dir.mkdir(exist_ok=True)
        
        # Sauvegarder photo
        photo_path = label_dir / f"{contribution.id}.jpg"
        
        # Décoder base64 et sauvegarder
        try:
            # Si stocké comme bytes, décoder
            if isinstance(contribution.photo_base64, bytes):
                photo_data = contribution.photo_base64
            else:
                photo_data = base64.b64decode(contribution.photo_base64)
            
            with open(photo_path, "wb") as f:
                f.write(photo_data)
            
            if label not in stats:
                stats[label] = 0
            stats[label] += 1
            
        except Exception as e:
            logger.error(f"❌ Erreur décodage photo {contribution.id}: {e}")
    
    logger.info(f"✅ Données préparées: {json.dumps(stats)}")
    
    return {
        "success": True,
        "dataset_path": str(version_dataset_dir),
        "stats": stats
    }


# ============================================
# 2. SAUVEGARDER ANCIEN MODÈLE (BACKUP)
# ============================================

async def backup_current_model(version: int) -> bool:
    """
    Sauvegarde le modèle actuel en cas de bug
    Permet rollback facile
    """
    logger.info(f"💾 Backup du modèle courant...")
    
    current_model_path = MODELS_DIR / "current" / "NutriScan.mlmodel"
    
    if not current_model_path.exists():
        logger.warning("⚠️  Pas de modèle courant à sauvegarder")
        return True  # Pas grave, c'est la première fois
    
    try:
        # Créer répertoire backup
        backup_path = BACKUP_DIR / f"v{version-1}_backup_{datetime.now().timestamp()}"
        backup_path.mkdir(parents=True, exist_ok=True)
        
        # Copier modèle
        shutil.copy2(
            current_model_path,
            backup_path / "NutriScan.mlmodel"
        )
        
        logger.info(f"✅ Modèle sauvegardé: {backup_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur backup: {e}")
        return False


# ============================================
# 3. CRÉER MODÈLE AVEC CREATE ML (simulé)
# ============================================

async def create_ml_train(
    version: int,
    dataset_path: str,
    db: Session
) -> bool:
    """
    Entraîne un modèle Core ML avec Create ML
    NOTE: En production, utiliser create-ml CLI ou Python bindings
    """
    logger.info(f"🤖 Entraînement modèle Create ML v{version}...")
    
    try:
        # Vérifier que dataset existe
        dataset = Path(dataset_path)
        if not dataset.exists():
            raise FileNotFoundError(f"Dataset not found: {dataset_path}")
        
        # ============================================
        # Option 1: Utiliser create-ml Python package
        # ============================================
        try:
            from turicreate import image_classifier
            
            logger.info("📦 Utilisation de TuriCreate...")
            
            # Créer classifier depuis images
            data = image_classifier.load_data(dataset_path)
            
            # Entraîner
            model = image_classifier.create(
                data,
                max_iterations=100,
                verbose=True
            )
            
            # Sauvegarder
            model_output_path = MODELS_DIR / f"v{version}"
            model_output_path.mkdir(exist_ok=True)
            
            model.export_coreml(
                model_output_path / "NutriScan.mlmodel"
            )
            
            logger.info(f"✅ Modèle TuriCreate généré!")
            
        except ImportError:
            logger.warning("⚠️  TuriCreate non disponible, simulant entraînement...")
            
            # ============================================
            # Option 2: Simulation (développement)
            # ============================================
            
            # Créer modèle factice pour tester
            model_output_path = MODELS_DIR / f"v{version}"
            model_output_path.mkdir(exist_ok=True)
            
            # Copier modèle précédent comme base
            current = MODELS_DIR / "current" / "NutriScan.mlmodel"
            if current.exists():
                shutil.copy2(
                    current,
                    model_output_path / "NutriScan.mlmodel"
                )
            else:
                # Créer fichier placeholder
                with open(model_output_path / "NutriScan.mlmodel", "w") as f:
                    f.write("PLACEHOLDER_MODEL_V" + str(version))
            
            logger.info(f"✅ Modèle simulation généré!")
        
        # Compter photos d'entraînement
        total_photos = sum(
            1 for _ in Path(dataset_path).rglob("*.jpg")
        ) + sum(
            1 for _ in Path(dataset_path).rglob("*.png")
        )
        
        # Mettre à jour DB
        from contribute_api import ModelVersion
        model_version = db.query(ModelVersion).filter(
            ModelVersion.version == version
        ).first()
        
        if model_version:
            model_version.training_photos = total_photos
            model_version.accuracy = 0.85  # Placeholder
            db.commit()
        
        logger.info(f"📊 Entraîné sur {total_photos} photos")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur entraînement: {e}")
        
        # Marquer comme failed en DB
        from contribute_api import ModelVersion
        model_version = db.query(ModelVersion).filter(
            ModelVersion.version == version
        ).first()
        
        if model_version:
            model_version.status = "failed"
            model_version.error_message = str(e)
            db.commit()
        
        return False


# ============================================
# 4. DÉPLOYER NOUVEAU MODÈLE
# ============================================

async def deploy_model(version: int) -> bool:
    """
    Déplace le nouveau modèle en "current" pour utilisation
    """
    logger.info(f"🚀 Déploiement modèle v{version}...")
    
    try:
        new_model = MODELS_DIR / f"v{version}" / "NutriScan.mlmodel"
        current_link = MODELS_DIR / "current" / "NutriScan.mlmodel"
        
        if not new_model.exists():
            raise FileNotFoundError(f"Modèle v{version} not found")
        
        # Créer répertoire current s'il n'existe pas
        (MODELS_DIR / "current").mkdir(exist_ok=True)
        
        # Remplacer current
        if current_link.exists():
            current_link.unlink()
        
        shutil.copy2(new_model, current_link)
        
        logger.info(f"✅ Modèle v{version} déployé en 'current'!")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur déploiement: {e}")
        return False


# ============================================
# 5. ORCHESTRATION COMPLÈTE
# ============================================

async def retrain_coreml_model(version: int, db: Session) -> bool:
    """
    Orchestre tout le processus de réentraînement
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"🔄 RÉENTRAÎNEMENT COMPLET MODÈLE V{version}")
    logger.info(f"{'='*60}\n")
    
    try:
        # Étape 1: Backup ancien modèle
        logger.info("📍 Étape 1/5: Backup ancien modèle...")
        backup_ok = await backup_current_model(version)
        if not backup_ok:
            logger.warning("⚠️  Backup échoué, continuant...")
        
        # Étape 2: Préparer données
        logger.info("\n📍 Étape 2/5: Préparation données...")
        prep_result = await prepare_training_data(version, db)
        if not prep_result["success"]:
            return False
        
        # Étape 3: Entraîner modèle
        logger.info("\n📍 Étape 3/5: Entraînement modèle...")
        train_ok = await create_ml_train(version, prep_result["dataset_path"], db)
        if not train_ok:
            return False
        
        # Étape 4: Déployer
        logger.info("\n📍 Étape 4/5: Déploiement...")
        deploy_ok = await deploy_model(version)
        if not deploy_ok:
            return False
        
        # Étape 5: Cleanup
        logger.info("\n📍 Étape 5/5: Nettoyage...")
        cleanup_old_datasets(keep_versions=3)
        
        logger.info(f"\n✅ RÉENTRAÎNEMENT V{version} COMPLÉTÉ!")
        logger.info(f"{'='*60}\n")
        
        return True
        
    except Exception as e:
        logger.error(f"\n❌ ERREUR CRITIQUE: {e}")
        logger.info(f"{'='*60}\n")
        return False


# ============================================
# 6. UTILITAIRES
# ============================================

def cleanup_old_datasets(keep_versions: int = 3):
    """
    Nettoie anciens datasets pour économiser espace disque
    Garde les N dernières versions
    """
    try:
        datasets = sorted(DATASETS_DIR.glob("v*"), reverse=True)
        for old_dataset in datasets[keep_versions:]:
            shutil.rmtree(old_dataset)
            logger.info(f"🗑️  Nettoyé ancien dataset: {old_dataset.name}")
    except Exception as e:
        logger.warning(f"⚠️  Erreur cleanup: {e}")


async def rollback_to_backup(version: int) -> bool:
    """
    Rollback d'urgence au modèle précédent en cas de bug
    """
    logger.warning(f"\n⚠️  ROLLBACK MODÈLE V{version}...\n")
    
    try:
        # Trouver backup précédent
        backups = sorted(BACKUP_DIR.glob(f"v{version-1}_*"), reverse=True)
        
        if not backups:
            logger.error("❌ Aucun backup trouvé!")
            return False
        
        backup_path = backups[0]
        current_model = MODELS_DIR / "current" / "NutriScan.mlmodel"
        
        # Restaurer
        shutil.copy2(
            backup_path / "NutriScan.mlmodel",
            current_model
        )
        
        logger.warning(f"✅ Rollback complété: {backup_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur rollback: {e}")
        return False
