import os
import shutil
import numpy as np
import zipfile
from pathlib import Path
from flask import current_app
from memimto.celery import celery
from memimto.models import db, Album, Image, Face
from sklearn.cluster import DBSCAN
from uuid import uuid4
import imghdr
import logging
from deepface import DeepFace
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, Dropout
from tensorflow.keras.callbacks import EarlyStopping
from sklearn.preprocessing import LabelEncoder
from tqdm import tqdm

def unzip(file_name, album_name):
    #Unzip the file
    data_dir = Path(current_app.config["data_dir"])
    image_list = []
    with zipfile.ZipFile(data_dir / file_name) as zip_file:
        for member in zip_file.namelist():
            filename = os.path.basename(member)
            # skip directories
            if not filename:
                continue
            
            extension = filename.split(".")[1]
            filename = str(uuid4()) + "." + extension
            source = zip_file.open(member)
            target = open(data_dir / filename, "wb")
            with source, target:
                shutil.copyfileobj(source, target)
            
            if imghdr.what(data_dir / filename):
                image_list.append(filename)
            else:
                os.remove(data_dir / filename)
    os.remove(data_dir / file_name)
    return image_list

def extract_face(image_list, album, db_session):
    data_dir = Path(current_app.config["data_dir"])
    faces = []

    for image_name in tqdm(image_list, desc="Extracting Faces", unit="image"):
        image_path = data_dir / image_name
        try:
            # Get the embeddings for all faces in the image
            # retinaface is the best for detecting faces, and Facenet is good for encoding
            face_data_list = DeepFace.represent(str(image_path), model_name='Facenet', detector_backend='retinaface', enforce_detection=False)
            
            # Create Image object outside the loop
            image_db = Image(name=image_name, album=album)
            db_session.add(image_db)
            db_session.commit()
            
            for face_data in face_data_list:
                # Extract the face encoding and box coordinates
                face_encoding = face_data['embedding']
                boxe = (face_data['facial_area']['y'], face_data['facial_area']['x'] + face_data['facial_area']['w'],
                        face_data['facial_area']['y'] + face_data['facial_area']['h'], face_data['facial_area']['x'])
                
                # Create a Face object and append it to the list
                face_db = Face(image=image_db, encoding=face_encoding, boxe=boxe)
                db_session.add(face_db)
                faces.append(face_db)
            db_session.commit()
        except Exception as e:
            print(f"Error processing {image_name}: {e}")
    return faces


def cluster(album_db, faces_db, db_session):
    encodings = np.array([face.encoding for face in faces_db])

    # Perform clustering using DBSCAN
    dbscan = DBSCAN(eps=0.15, min_samples=3, metric='cosine') # This is the trickiest part to adjust
    labels = dbscan.fit_predict(encodings)

    # Get cluster labels and statistics
    num_faces = len(labels)  # Total number of faces found
    num_clusters = len(np.unique(labels)) - 1  # Number of clusters detected (excluding noise)
    print(f"{num_faces} faces detected in {num_clusters} clusters.")

    # Update face database with cluster labels
    for i, label in enumerate(labels):
        faces_db[i].cluster = label

    # Commit changes to the database
    db_session.commit()

    # Define and train a TensorFlow classifier based on the results from the clustering
    num_classes = len(np.unique(labels))
    encoder = LabelEncoder()
    labels_encoded = encoder.fit_transform(labels)

    model = Sequential([
        Dense(128, activation='relu', input_shape=(encodings.shape[1],)),
        Dropout(0.5),
        Dense(128, activation='relu'),
        Dropout(0.5),
        Dense(64, activation='relu'),
        Dropout(0.5),
        Dense(num_classes, activation='softmax')
    ])

    model.compile(optimizer='adam', loss='sparse_categorical_crossentropy', metrics=['accuracy'])
    early_stopping = EarlyStopping(patience=5, restore_best_weights=True)
    model.fit(encodings, labels_encoded, epochs=100, batch_size=16, callbacks=[early_stopping])

    # Save the trained model
    classifier_name = str(uuid4()) + ".h5"
    album_db.classifier = classifier_name
    model.save(current_app.config["data_dir"] / classifier_name)

    # Commit changes to the database
    db_session.commit()

    print("Classifier saved successfully.")
    print(f"{num_faces} faces detected in {num_clusters} clusters.")


@celery.task()
def re_cluster_album(album_id):
    try:
        album = Album.query.get_or_404(album_id)
        print(f"Reclustering album {album.name}")
        faces = []
        for image in album.images:
            faces.extend(image.faces)

        cluster(album, faces, db.session)
        print("Done")
    except Exception as e:
        logging.exception(e)

@celery.task()
def new_album(file_name):
    try:
        album_name = file_name.split(".")[0]
        print(f"New album : {album_name}")
        db_session = db.session
        album = Album(name=album_name.title())
        db_session.add(album)
        db_session.commit()
        print(f"Unzip : {album_name}")
        images_list = unzip(file_name, album_name)
        print(f"Extract and encode face : {album_name}")
        faces = extract_face(images_list, album, db_session)
        print(f"Clustering face : {album_name}")
        cluster(album, faces, db_session)
        print(f"Album {album_name} done")
    except Exception as e:
        logging.exception(e)
