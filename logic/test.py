import streamlit as st
import folium
from folium.plugins import Draw
from shapely.geometry import shape
import streamlit_folium

def run_app():
    st.set_page_config(layout="wide")
    st.title("Draw smoke test")

    m = folium.Map(location=[52.52, 13.405], zoom_start=14)
    m.options['doubleClickZoom'] = False  # optional

    Draw(
        export=False,
        draw_options={"polygon": True, "rectangle": True, "polyline": False, "circle": False, "circlemarker": False, "marker": False},
        edit_options={"edit": False, "remove": False},
    ).add_to(m)

    data = streamlit_folium.st_folium(m, height=500, key="smoke", returned_objects=["last_object_drawn"])
    st.write("Raw draw payload:", data)

    geo = (data or {}).get("last_object_drawn", {}).get("geometry")
    if geo:
        try:
            poly = shape(geo)
            st.success(f"Captured geometry with {len(poly.exterior.coords)} coords.")
        except Exception as e:
            st.error(f"Parse error: {e}")

if __name__ == "__main__":
    run_app()