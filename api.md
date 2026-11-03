
# Smart Travel Planner – API Documentation

**Backend:** Django + Django REST Framework  
**Database:** PostgreSQL  
**Authentication:** JWT (Simple JWT)  
**Base URL:** `http://127.0.0.1:8000`

This document helps frontend developers integrate the Smart Travel Planner backend with the React frontend.

---

## 1. Authentication APIs

### 1.1 User Registration

- **Method:** POST
- **Endpoint:** `/api/accounts/register/`
- **Authentication:** Not required

**Request Body:**

```json
{
  "username": "lav",
  "email": "lav@example.com",
  "password": "StrongPassword123"
}
```

**Successful Response – 201 Created:**

```json
{
  "id": 1,
  "username": "lav",
  "email": "lav@example.com"
}
```

### 1.2 User Login – Obtain JWT Token

- **Method:** POST
- **Endpoint:** `/api/token/`
- **Authentication:** Not required

**Request Body:**

```json
{
  "username": "lav",
  "password": "StrongPassword123"
}
```

**Successful Response – 200 OK:**

```json
{
  "refresh": "your_refresh_token",
  "access": "your_access_token"
}
```

Save the access token on the frontend and use it for authenticated requests.

### 1.3 Refresh JWT Token

- **Method:** POST
- **Endpoint:** `/api/token/refresh/`
- **Authentication:** Refresh token required

**Request Body:**

```json
{
  "refresh": "your_refresh_token"
}
```

**Response:**

```json
{
  "access": "new_access_token"
}
```

---

## 2. Trip APIs

All trip endpoints require JWT authentication.

**Header:**

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

### 2.1 Create a Trip

- **Method:** POST
- **Endpoint:** `/api/trips/`

**Request Body:**

```json
{
  "source": "Delhi",
  "destination": "Manali",
  "start_date": "2026-12-15",
  "end_date": "2026-12-20",
  "number_of_people": 2,
  "estimated_budget": "20000.00",
  "travel_mode": "self_vehicle"
}
```

The backend automatically associates the trip with the authenticated user.

### 2.2 Get All Trips

- **Method:** GET
- **Endpoint:** `/api/trips/`

Returns trips belonging to the authenticated user.

### 2.3 Get a Single Trip

- **Method:** GET
- **Endpoint:** `/api/trips/{id}/`

Example:

`/api/trips/1/`

### 2.4 Update a Trip

- **Method:** PUT or PATCH
- **Endpoint:** `/api/trips/{id}/`

### 2.5 Delete a Trip

- **Method:** DELETE
- **Endpoint:** `/api/trips/{id}/`

---

## 3. Destination APIs

These endpoints are publicly accessible.

### 3.1 Get All Destinations

- **Method:** GET
- **Endpoint:** `/api/destinations/`

### 3.2 Get a Single Destination

- **Method:** GET
- **Endpoint:** `/api/destinations/{id}/`

### 3.3 Get All Places

- **Method:** GET
- **Endpoint:** `/api/places/`

### 3.4 Get a Single Place

- **Method:** GET
- **Endpoint:** `/api/places/{id}/`

**Example Destination Response:**

```json
{
  "id": 1,
  "name": "Manali",
  "state": "Himachal Pradesh",
  "country": "India",
  "description": "A popular hill station",
  "places": [
    {
      "id": 1,
      "destination": 1,
      "name": "Hadimba Devi Temple",
      "category": "attraction",
      "latitude": "32.2432",
      "longitude": "77.1892",
      "time_required": 60,
      "estimated_cost": "0.00",
      "description": "Popular temple in Manali"
    }
  ]
}
```

---

## 4. Transport Option APIs

All transport endpoints require JWT authentication.

### 4.1 Get Transport Options

- **Method:** GET
- **Endpoint:** `/api/transport-options/`

### 4.2 Add Transport Option

- **Method:** POST
- **Endpoint:** `/api/transport-options/`

**Request Body:**

```json
{
  "trip": 1,
  "transport_type": "bus",
  "origin": "Delhi",
  "destination": "Manali",
  "departure_time": "2026-12-15T08:00:00",
  "arrival_time": "2026-12-15T18:00:00",
  "estimated_cost": "1200.00",
  "provider": "Example Bus Provider",
  "booking_url": "https://example.com"
}
```

### 4.3 Get, Update or Delete a Transport Option

- **GET:** `/api/transport-options/{id}/`
- **PUT/PATCH:** `/api/transport-options/{id}/`
- **DELETE:** `/api/transport-options/{id}/`

---

## 5. Accommodation APIs

All accommodation endpoints require JWT authentication.

### 5.1 Get Accommodation Options

- **Method:** GET
- **Endpoint:** `/api/accommodations/`

### 5.2 Add Accommodation

- **Method:** POST
- **Endpoint:** `/api/accommodations/`

**Request Body:**

```json
{
  "trip": 1,
  "name": "Manali Mountain Hotel",
  "accommodation_type": "hotel",
  "location": "Old Manali",
  "price_per_night": "2000.00",
  "rating": "4.50",
  "booking_url": "https://example.com"
}
```

### 5.3 Get, Update or Delete Accommodation

- **GET:** `/api/accommodations/{id}/`
- **PUT/PATCH:** `/api/accommodations/{id}/`
- **DELETE:** `/api/accommodations/{id}/`

---

## 6. Frontend Integration Guide

### Step 1: Login

Call:

`POST /api/token/`

Store the returned access token.

### Step 2: Send Authenticated Requests

Include the following headers:

```javascript
const headers = {
  "Content-Type": "application/json",
  "Authorization": `Bearer ${accessToken}`
};
```

### Step 3: Example – Fetch User Trips

```javascript
const response = await fetch(
  "http://127.0.0.1:8000/api/trips/",
  {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${accessToken}`
    }
  }
);

const data = await response.json();
console.log(data);
```

### Step 4: Example – Create a Trip

```javascript
const response = await fetch(
  "http://127.0.0.1:8000/api/trips/",
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${accessToken}`
    },
    body: JSON.stringify({
      source: "Delhi",
      destination: "Manali",
      start_date: "2026-12-15",
      end_date: "2026-12-20",
      number_of_people: 2,
      estimated_budget: "20000.00",
      travel_mode: "self_vehicle"
    })
  }
);

const data = await response.json();
console.log(data);
```

---

## 7. Common HTTP Status Codes

| Status Code | Meaning |
|---|---|
| 200 | Request successful |
| 201 | Resource created |
| 204 | Resource deleted successfully |
| 400 | Bad request / validation error |
| 401 | Authentication required or invalid token |
| 403 | Permission denied |
| 404 | Resource not found |
| 500 | Internal server error |

---

## 8. Important Notes

1. Register a user before attempting login.
2. Obtain a JWT access token before accessing trip, transport or accommodation APIs.
3. Destination and place APIs are publicly accessible.
4. Each authenticated user can access their own trips.
5. The backend currently runs locally at `http://127.0.0.1:8000`.
6. For frontend testing on the same computer, React can use this local base URL.
7. When the backend is deployed, replace the base URL with the deployed backend URL.
8. Never expose passwords, JWT signing keys, or secret credentials in frontend code or GitHub.

---

## 9. APIs and Features Planned for Future Implementation

The following features are part of the project plan but are not yet available as working API endpoints:

- Cost calculation and budget estimation.
- Economy, Balanced and Convenience plan generation.
- Route optimization.
- Nearby places search using geographical distance.
- Automatic day-wise itinerary generation.
- Itinerary customization.
- Google Maps / Places integration.
- Flight, train and bus API integration.
- Hotel recommendation integration.
- Google Calendar integration.

These endpoints will be documented here once implemented.

---

**Project:** Smart Travel Planner  
**Backend Framework:** Django REST Framework  
**Frontend Framework:** React.js  
**Database:** PostgreSQL  
**Documentation:** API.md
