import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score

# 1. 가상 데이터 생성
data = {
    'age': [22, 45, 33, 35, 52, 23, 43, 56, 48, 29, 33, 53, 56, 58, 29],
    'monthly_spend': [10, 200, 100, 150, 300, 15, 180, 400, 250, 35, 150, 300, 15, 180,99],
    'months_active': [1, 36, 24, 30, 60, 2, 33, 72, 50, 5, 33, 72, 50, 5,12],
    'churn': [1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1]
}

df = pd.DataFrame(data)

X = df[['age', 'monthly_spend', 'months_active']]
y = df['churn']

# 2. 데이터 분리
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

# 3. 모델 생성 및 학습
clf = RandomForestClassifier(n_estimators=100, random_state=42)
clf.fit(X_train, y_train)

# 4. 예측 및 평가
y_pred = clf.predict(X_test)

print("[완료] 정확도:", accuracy_score(y_test, y_pred))
print("[통계] 리포트:\n", classification_report(y_test, y_pred))
