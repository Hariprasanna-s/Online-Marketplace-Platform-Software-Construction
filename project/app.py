from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import mysql.connector
from mysql.connector import Error
import os
import base64

app = Flask(__name__, template_folder="templates")
CORS(app)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
ALLOWED_MIMES      = {"image/png", "image/jpeg", "image/jpg"}
MIN_RATING, MAX_RATING = 1, 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB (becomes ~6.7MB when base64 encoded)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "4321"),
    "database": os.getenv("DB_NAME", "ecommerce_db"),
}


# ── Helpers ────────────────────────────────────────────────

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def allowed_file(filename: str, mime: str) -> bool:
    ext_ok  = "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    mime_ok = mime in ALLOWED_MIMES
    return ext_ok and mime_ok

def product_exists(cursor, product_id: int) -> bool:
    cursor.execute("SELECT id FROM products WHERE id = %s", (product_id,))
    return cursor.fetchone() is not None

def row_to_dict(row: dict) -> dict:
    """Convert a DB row to a JSON-serialisable dict."""
    out = {}
    for k, v in row.items():
        out[k] = v
    return out


# ── Routes ─────────────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

@app.route("/add_product", methods=["POST"])
def add_product():
    # Accept multipart/form-data so image can be sent together with product data
    title       = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    price_raw   = request.form.get("price")
    stock_raw   = request.form.get("stock")
    file        = request.files.get("image")

    if not title or not price_raw:
        return jsonify({"error": "Title and Price are required"}), 400

    try:
        price = float(price_raw)
        if price < 0: raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a non-negative number"}), 400

    stock = None
    if stock_raw and stock_raw.strip():
        try:
            stock = int(stock_raw)
            if stock < 0: raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "Stock must be a non-negative integer"}), 400

    image_data, image_mime, image_name = None, None, None
    if file and file.filename:
        if not allowed_file(file.filename, file.content_type):
            return jsonify({"error": "Invalid image type. Allowed: PNG, JPG, JPEG"}), 400
        raw = file.read()
        if len(raw) > MAX_IMAGE_BYTES:
            return jsonify({"error": "Image too large. Max 5 MB"}), 400
        image_data = base64.b64encode(raw).decode("utf-8")
        image_mime = file.content_type
        image_name = file.filename

    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """INSERT INTO products (title, description, price, stock, image_data, image_mime, image_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (title, description, price, stock, image_data, image_mime, image_name)
        )
        conn.commit()
        new_id = cursor.lastrowid

        # Return product without raw blob (use /product_image/<id> to fetch image)
        cursor.execute("""
            SELECT id, title, description, price, stock, image_name, image_mime, created_at
            FROM products WHERE id = %s
        """, (new_id,))
        return jsonify({"message": "Product created", "product": cursor.fetchone()}), 201
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close(); conn.close()


@app.route("/get_products", methods=["GET"])
def get_products():
    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT
                p.id, p.title, p.description, p.price, p.stock,
                p.image_name, p.image_mime, p.created_at,
                ROUND(AVG(r.rating), 2) AS avg_rating,
                COUNT(r.id)             AS rating_count,
                CASE WHEN p.image_data IS NOT NULL THEN 1 ELSE 0 END AS has_image
            FROM products p
            LEFT JOIN ratings r ON r.product_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
        """)
        return jsonify(cursor.fetchall())
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close(); conn.close()


@app.route("/product_image/<int:product_id>", methods=["GET"])
def product_image(product_id: int):
    """Serve the product image as a base64 data-URI string."""
    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT image_data, image_mime FROM products WHERE id = %s", (product_id,)
        )
        row = cursor.fetchone()
        if not row or not row["image_data"]:
            return jsonify({"error": "No image"}), 404

        b64 = row["image_data"]
        return jsonify({
            "data_uri": f"data:{row['image_mime']};base64,{b64}"
        })
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close(); conn.close()


@app.route("/edit_product/<int:product_id>", methods=["PUT"])
def edit_product(product_id: int):
    # Support both JSON (text fields only) and multipart (with image)
    is_multipart = request.content_type and "multipart" in request.content_type

    if is_multipart:
        data_src = request.form
    else:
        data_src = request.get_json(silent=True) or {}

    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        if not product_exists(cursor, product_id):
            return jsonify({"error": "Product not found"}), 404

        fields, values = [], []

        if "title" in data_src:
            t = (data_src["title"] or "").strip()
            if not t: return jsonify({"error": "Title cannot be empty"}), 400
            fields.append("title = %s"); values.append(t)

        if "description" in data_src:
            fields.append("description = %s")
            values.append((data_src["description"] or "").strip() or None)

        if "price" in data_src:
            try:
                p = float(data_src["price"])
                if p < 0: raise ValueError
                fields.append("price = %s"); values.append(p)
            except (TypeError, ValueError):
                return jsonify({"error": "Price must be a non-negative number"}), 400

        if "stock" in data_src:
            sv = data_src["stock"]
            if sv == "" or sv is None:
                fields.append("stock = %s"); values.append(None)
            else:
                try:
                    s = int(sv)
                    if s < 0: raise ValueError
                    fields.append("stock = %s"); values.append(s)
                except (TypeError, ValueError):
                    return jsonify({"error": "Stock must be a non-negative integer"}), 400

        # Handle image replacement
        file = request.files.get("image") if is_multipart else None
        if file and file.filename:
            if not allowed_file(file.filename, file.content_type):
                return jsonify({"error": "Invalid image type"}), 400
            raw = file.read()
            if len(raw) > MAX_IMAGE_BYTES:
                return jsonify({"error": "Image too large. Max 5 MB"}), 400
            fields += ["image_data = %s", "image_mime = %s", "image_name = %s"]
            values += [base64.b64encode(raw).decode("utf-8"), file.content_type, file.filename]

        if not fields:
            return jsonify({"error": "No fields to update"}), 400

        values.append(product_id)
        cursor.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = %s", values)
        conn.commit()

        cursor.execute("""
            SELECT p.id, p.title, p.description, p.price, p.stock,
                   p.image_name, p.image_mime, p.created_at,
                   ROUND(AVG(r.rating), 2) AS avg_rating, COUNT(r.id) AS rating_count,
                   CASE WHEN p.image_data IS NOT NULL THEN 1 ELSE 0 END AS has_image
            FROM products p LEFT JOIN ratings r ON r.product_id = p.id
            WHERE p.id = %s GROUP BY p.id
        """, (product_id,))
        return jsonify({"message": "Product updated", "product": cursor.fetchone()})
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close(); conn.close()


@app.route("/delete_product/<int:product_id>", methods=["DELETE"])
def delete_product(product_id: int):
    try:
        conn   = get_db()
        cursor = conn.cursor()
        if not product_exists(cursor, product_id):
            return jsonify({"error": "Product not found"}), 404
        cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
        conn.commit()
        return jsonify({"message": "Product deleted"})
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close(); conn.close()


@app.route("/rate_product/<int:product_id>", methods=["POST"])
def rate_product(product_id: int):
    data   = request.get_json(silent=True) or {}
    rating = data.get("rating")
    try:
        rating = int(rating)
        if not (MIN_RATING <= rating <= MAX_RATING): raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": f"Rating must be between {MIN_RATING} and {MAX_RATING}"}), 400

    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        if not product_exists(cursor, product_id):
            return jsonify({"error": "Product not found"}), 404
        cursor.execute("INSERT INTO ratings (product_id, rating) VALUES (%s, %s)", (product_id, rating))
        conn.commit()
        cursor.execute(
            "SELECT ROUND(AVG(rating), 2) AS avg_rating FROM ratings WHERE product_id = %s",
            (product_id,)
        )
        return jsonify({"message": "Rating submitted", "average_rating": cursor.fetchone()["avg_rating"]})
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close(); conn.close()


if __name__ == "__main__":
    app.run(debug=True)