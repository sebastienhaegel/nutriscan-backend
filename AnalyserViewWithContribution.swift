"""
Onglet Analyser avec intégration du système d'apprentissage centralisé
Quand Claude reconnaît → contribution automatique au serveur
"""

import SwiftUI
import CoreML
import Vision
import PhotosUI

struct AnalyserView: View {
    @StateObject private var vm = NutriScanViewModel()
    @StateObject private var learningVM = NutriScanLearningViewModel()
    
    @State private var showCamera = false
    @State private var pickerItem: PhotosPickerItem?
    @State private var scannedImage: UIImage?
    @State private var analysisResult: AnalysisResult?
    @State private var isAnalyzing = false
    @State private var showLearningStats = false
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                // Header
                HStack {
                    Text("📷 Analyser")
                        .font(.title2)
                        .fontWeight(.bold)
                    
                    Spacer()
                    
                    // Badge de mise à jour modèle
                    if learningVM.modelUpdateAvailable {
                        Label("Mise à jour", systemImage: "arrow.down.circle.fill")
                            .font(.caption)
                            .foregroundColor(.green)
                    }
                    
                    Text("v\(learningVM.modelVersion)")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
                .padding(.horizontal)
                
                // Affichage image scannée
                if let image = scannedImage {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(height: 250)
                        .cornerRadius(12)
                        .padding()
                } else {
                    RoundedRectangle(cornerRadius: 12)
                        .fill(Color.gray.opacity(0.2))
                        .frame(height: 250)
                        .overlay(
                            VStack(spacing: 12) {
                                Image(systemName: "camera.fill")
                                    .font(.system(size: 40))
                                    .foregroundColor(.gray)
                                Text("Scannez un repas")
                                    .font(.headline)
                            }
                        )
                        .padding()
                }
                
                // Boutons action
                HStack(spacing: 15) {
                    // Caméra
                    Button(action: { showCamera = true }) {
                        Label("Caméra", systemImage: "camera.fill")
                            .frame(maxWidth: .infinity)
                            .padding()
                            .background(Color.blue)
                            .foregroundColor(.white)
                            .cornerRadius(8)
                    }
                    
                    // Photothèque
                    PhotosPicker(
                        selection: $pickerItem,
                        matching: .images,
                        photoLibrary: .shared()
                    ) {
                        Label("Galerie", systemImage: "photo.fill")
                            .frame(maxWidth: .infinity)
                            .padding()
                            .background(Color.green)
                            .foregroundColor(.white)
                            .cornerRadius(8)
                    }
                }
                .padding(.horizontal)
                
                // Résultat analyse
                if let result = analysisResult {
                    VStack(alignment: .leading, spacing: 15) {
                        HStack {
                            Text("📊 Résultat")
                                .font(.headline)
                            
                            Spacer()
                            
                            // Badge contribution
                            if result.wasContributed {
                                Label("Contribué", systemImage: "checkmark.circle.fill")
                                    .font(.caption)
                                    .foregroundColor(.green)
                            }
                        }
                        
                        Divider()
                        
                        // Label
                        HStack {
                            Text("Plat:")
                                .fontWeight(.semibold)
                            Spacer()
                            Text(result.label)
                                .font(.headline)
                        }
                        
                        // Calories
                        HStack {
                            Text("Calories:")
                                .fontWeight(.semibold)
                            Spacer()
                            Text("\(Int(result.calories)) kcal")
                        }
                        
                        // Macros
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Macronutriments:")
                                .fontWeight(.semibold)
                            
                            HStack(spacing: 20) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("Protéines")
                                        .font(.caption)
                                        .foregroundColor(.gray)
                                    Text("\(Int(result.protein))g")
                                        .fontWeight(.semibold)
                                }
                                
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("Glucides")
                                        .font(.caption)
                                        .foregroundColor(.gray)
                                    Text("\(Int(result.carbs))g")
                                        .fontWeight(.semibold)
                                }
                                
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("Lipides")
                                        .font(.caption)
                                        .foregroundColor(.gray)
                                    Text("\(Int(result.fat))g")
                                        .fontWeight(.semibold)
                                }
                            }
                        }
                    }
                    .padding()
                    .background(Color.gray.opacity(0.1))
                    .cornerRadius(12)
                    .padding(.horizontal)
                }
                
                // Message contribution
                if !learningVM.contributionMessage.isEmpty {
                    HStack {
                        Image(systemName: "info.circle.fill")
                        Text(learningVM.contributionMessage)
                            .font(.caption)
                    }
                    .padding()
                    .background(Color.blue.opacity(0.1))
                    .foregroundColor(.blue)
                    .cornerRadius(8)
                    .padding(.horizontal)
                }
                
                // Bouton stats d'apprentissage
                Button(action: { showLearningStats = true }) {
                    HStack {
                        Image(systemName: "chart.bar.fill")
                        Text("Voir stats apprentissage")
                    }
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(Color.purple.opacity(0.2))
                    .foregroundColor(.purple)
                    .cornerRadius(8)
                }
                .padding(.horizontal)
                
                if isAnalyzing {
                    ProgressView("Analyse en cours...")
                }
                
                Spacer()
            }
            .navigationTitle("Analyser")
            .sheet(isPresented: $showCamera) {
                CameraViewControllerRepresentable { image in
                    scannedImage = image
                    Task { await analyzeImage(image) }
                }
            }
            .sheet(isPresented: $showLearningStats) {
                LearningStatsView(vm: learningVM)
            }
            .onChange(of: pickerItem) { oldValue, newValue in
                Task {
                    if let data = try? await newValue?.loadTransferable(type: Data.self),
                       let image = UIImage(data: data) {
                        scannedImage = image
                        await analyzeImage(image)
                    }
                }
            }
            .onAppear {
                // Vérifier mise à jour modèle au chargement
                Task {
                    await learningVM.checkForModelUpdate()
                    await learningVM.loadLearningStats()
                }
            }
        }
    }
    
    
    // MARK: - Analyse et contribution
    
    private func analyzeImage(_ image: UIImage) async {
        """
        Analyse une image :
        1. Essayer Core ML local
        2. Si échoue → Claude reconnaît
        3. ✨ Contribuer au serveur central pour apprentissage
        """
        
        DispatchQueue.main.async {
            self.isAnalyzing = true
            self.analysisResult = nil
        }
        
        // Convertir image en JPEG
        guard let imageData = image.jpegData(compressionQuality: 0.8) else {
            DispatchQueue.main.async {
                self.isAnalyzing = false
                self.learningVM.contributionMessage = "❌ Erreur compression image"
            }
            return
        }
        
        do {
            // 1️⃣ ESSAYER CORE ML LOCAL
            var coreMLLabel: String? = nil
            var coreMLConfidence: Float = 0.0
            
            if let prediction = try vm.predictWithCoreML(image) {
                coreMLLabel = prediction.label
                coreMLConfidence = prediction.confidence
                
                // Si confiance haute, utiliser
                if coreMLConfidence > 0.7 {
                    DispatchQueue.main.async {
                        self.analysisResult = AnalysisResult(
                            label: prediction.label,
                            calories: prediction.calories,
                            protein: prediction.protein,
                            carbs: prediction.carbs,
                            fat: prediction.fat,
                            wasContributed: false
                        )
                        self.isAnalyzing = false
                    }
                    return
                }
            }
            
            // 2️⃣ CORE ML ÉCHOUE → DEMANDER À CLAUDE
            print("📤 Core ML échoue, demandant à Claude...")
            
            let claudeResult = try await vm.analyzeFoodWithClaude(
                imageData: imageData,
                fallbackLabel: coreMLLabel
            )
            
            DispatchQueue.main.async {
                self.analysisResult = AnalysisResult(
                    label: claudeResult.label,
                    calories: claudeResult.calories,
                    protein: claudeResult.protein,
                    carbs: claudeResult.carbs,
                    fat: claudeResult.fat,
                    wasContributed: false
                )
            }
            
            // 3️⃣ ✨ CONTRIBUER AU SERVEUR CENTRAL
            print("🚀 Contribution au système d'apprentissage...")
            
            await learningVM.contributePhoto(
                imageData: imageData,
                label: claudeResult.label,
                claudeConfidence: 0.9
            )
            
            DispatchQueue.main.async {
                self.analysisResult?.wasContributed = true
                self.isAnalyzing = false
            }
            
        } catch {
            DispatchQueue.main.async {
                self.learningVM.contributionMessage = "❌ Erreur: \(error.localizedDescription)"
                self.isAnalyzing = false
            }
        }
    }
}


// MARK: - Modèles

struct AnalysisResult {
    let label: String
    let calories: Float
    let protein: Float
    let carbs: Float
    let fat: Float
    var wasContributed: Bool = false
}


// MARK: - Stats View

struct LearningStatsView: View {
    @ObservedObject var vm: NutriScanLearningViewModel
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                if let stats = vm.learningStats {
                    // Stats globales
                    VStack(spacing: 15) {
                        StatRow(label: "Total contributions", value: "\(stats.total_contributions)")
                        StatRow(label: "Utilisateurs actifs", value: "\(stats.active_users)")
                        StatRow(label: "Version modèle", value: "v\(stats.current_model_version)")
                        StatRow(label: "Statut", value: stats.model_status)
                    }
                    .padding()
                    .background(Color.gray.opacity(0.1))
                    .cornerRadius(12)
                    .padding()
                    
                    // Par label
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Top aliments reconnus")
                            .font(.headline)
                            .padding(.horizontal)
                        
                        ForEach(stats.labels.sorted { $0.count > $1.count }.prefix(10), id: \.label) { stat in
                            HStack {
                                Text(stat.label)
                                    .capitalize()
                                Spacer()
                                Text("\(stat.count) photos")
                                    .fontWeight(.semibold)
                            }
                            .padding()
                            .background(Color.gray.opacity(0.05))
                            .cornerRadius(8)
                        }
                    }
                    .padding()
                } else {
                    ProgressView("Chargement stats...")
                }
                
                Spacer()
            }
            .navigationTitle("Apprentissage")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Fermer") { dismiss() }
                }
            }
            .onAppear {
                Task { await vm.loadLearningStats() }
            }
        }
    }
}

struct StatRow: View {
    let label: String
    let value: String
    
    var body: some View {
        HStack {
            Text(label)
                .foregroundColor(.gray)
            Spacer()
            Text(value)
                .fontWeight(.semibold)
        }
    }
}


// MARK: - Camera

struct CameraViewControllerRepresentable: UIViewControllerRepresentable {
    let onImageCaptured: (UIImage) -> Void
    
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let controller = UIImagePickerController()
        controller.sourceType = .camera
        controller.delegate = context.coordinator
        return controller
    }
    
    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}
    
    func makeCoordinator() -> Coordinator {
        Coordinator(onImageCaptured: onImageCaptured)
    }
    
    class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onImageCaptured: (UIImage) -> Void
        
        init(onImageCaptured: @escaping (UIImage) -> Void) {
            self.onImageCaptured = onImageCaptured
        }
        
        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey : Any]
        ) {
            if let image = info[.originalImage] as? UIImage {
                onImageCaptured(image)
            }
            picker.dismiss(animated: true)
        }
    }
}
